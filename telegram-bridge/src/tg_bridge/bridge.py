from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import stat

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ReactionTypeEmoji, ReplyParameters
from .rpc import Rpc, RpcError
from .store import Store
from .telegram import Sender, DownloadRejected, download_event, codex_input

log = logging.getLogger('tg_bridge')


class Bridge:
    def __init__(self,cfg,store,bot,rpc):
        self.cfg,self.store,self.bot,self.rpc = cfg,store,bot,rpc
        self.sender = Sender(cfg,store,bot)
        self.resumed = False
        self.connection = None
        self.dispatch_lock = asyncio.Lock()
        self.ipc_tasks = set()
        self.reconcile_after = {}

    async def attach(self):
        await self.rpc.connect()
        connection = getattr(self.rpc,'websocket',None)
        if self.resumed and self.connection is connection:
            return
        thread = (await self.rpc.call('thread/read',{'threadId':self.cfg.thread_id}))['thread']
        if thread.get('cwd') and os.path.realpath(thread['cwd']) != str(self.cfg.workspace):
            raise ValueError('Thread workspace does not match configured WORKSPACE')
        result = await self.rpc.call('thread/resume',{'threadId':self.cfg.thread_id,'excludeTurns':True,**self.cfg.thread_params()},timeout=120)
        if result['thread']['id'] != self.cfg.thread_id:
            raise ValueError('App Server returned an unexpected thread')
        self.resumed,self.connection = True,connection
        log.info('Codex attached; thread=%s',self.cfg.thread_id)

    async def poll(self):
        backoff = 1
        while True:
            try:
                updates = await self.bot.get_updates(offset=int(self.store.get('offset','0')),timeout=40,
                    allowed_updates=['message'],request_timeout=50)
                for update in updates:
                    if update.update_id < int(self.store.get('offset','0')):
                        continue
                    message = update.message
                    human = message and message.from_user and not message.from_user.is_bot
                    if human and message.chat.id != self.cfg.chat_id:
                        try:
                            await self.bot.send_message(chat_id=message.chat.id,
                                text=f'чат {message.chat.id} не разрешен',request_timeout=5,
                                reply_parameters=ReplyParameters(message_id=message.message_id,allow_sending_without_reply=True))
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            # A foreign chat's permissions/rate limit/network failure
                            # must not block the allowed chat or trigger Codex work.
                            log.warning('Unauthorized chat notice unavailable (%s)',type(error).__name__)
                    allowed = human and self.cfg.authorized(message.chat.id,message.from_user.id)
                    self.store.ingest(update.update_id,message if allowed else None)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Telegram polling unavailable (%s)',type(error).__name__)
                await asyncio.sleep(backoff);backoff = min(30,backoff*2)

    async def download_once(self):
        row = self.store.download_head()
        if not row or (row['download_retry_at'] or 0)>self.store.clock():
            return False
        seq = row['seq']
        if row['status'] != 'processed':
            self.store.transition(seq,'downloading',attempts=row['attempts']+1)
        try:
            event = await download_event(self.bot,self.cfg,row['raw'])
            self.store.downloaded(seq,event)
        except (DownloadRejected,TelegramBadRequest,TelegramForbiddenError) as error:
            if self.store.row(seq)['status'] != 'processed':
                self.store.transition(seq,'failed',reason='attachment rejected',error=type(error).__name__)
            else:
                self.store.update(seq,event=json.dumps({**self.store._raw_event(row['raw']),'attachment_error':type(error).__name__}))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            delay = min(30,2**min(row['attempts'],5))
            self.store.update(seq,error=type(error).__name__,download_retry_at=self.store.clock()+delay)
            log.warning('Download seq=%s waiting (%s)',seq,type(error).__name__)
        return True

    async def downloader(self):
        while True:
            await self.download_once()
            await asyncio.sleep(0.2)

    @staticmethod
    def marker(row,reminder=False):
        return f"[telegram-{'reminder' if reminder else 'event'}:{row['chat']}:{row['message']}]"

    async def reconcile(self,marker,reminder=False):
        expected = ('напоминаю, если забыл\n' if reminder else '')+marker+'\n'
        async for turn in self.rpc.pages('thread/turns/list',threadId=self.cfg.thread_id,sortDirection='desc',itemsView='full'):
            for item in turn.get('items',[]):
                if item.get('type') == 'userMessage':
                    for content in item.get('content',[]):
                        if content.get('text','').startswith(expected):
                            return turn['id']
        return None

    async def active_turn(self):
        result = await self.rpc.call('thread/turns/list',{'threadId':self.cfg.thread_id,'sortDirection':'desc','itemsView':'notLoaded','limit':1})
        for turn in result['data']:
            if turn['status']=='inProgress':
                return turn['id']
        return None

    @staticmethod
    def retryable_rpc(error):
        message = error.details.get('message','').lower()
        return error.code == -32001 or (error.code in {-32600,-32602} and any(
            phrase in message for phrase in ('no active turn','active turn mismatch','expected turn','expectedturnid',
                'turn is not active','turn is no longer active','already has an active turn','turn already active',
                'turn is already active','turn already in progress','turn is already in progress','active turn already exists','thread is busy')))

    async def submit(self,seq,reminder=False):
        async with self.dispatch_lock:
            row = self.store.row(seq)
            if row['status'] != ('processing' if reminder else 'ready'):
                return True
            state_field = 'reminder_state' if reminder else 'dispatch_state'
            at_field = 'reminder_at' if reminder else 'dispatch_at'
            if row[state_field]=='sent':
                return True
            recovery_key = (seq,reminder)
            if row[state_field] in {'inflight','uncertain'} and self.reconcile_after.get(recovery_key,0)>self.store.clock():
                return False
            await self.attach()
            marker = self.marker(row,reminder)
            if row[state_field] in {'inflight','uncertain'}:
                turn = await self.reconcile(marker,reminder)
                if turn:
                    self.store.delivered_input(seq,turn,reminder,at=row[at_field])
                    self.reconcile_after.pop(recovery_key,None)
                    return True
                self.store.update(seq,**{state_field:'uncertain','error':'Codex acknowledgement missing; local resolution required'})
                self.reconcile_after[recovery_key] = self.store.clock()+15
                return False
            event = json.loads(row['event'])
            for _ in range(2):
                if self.store.row(seq)['status'] != ('processing' if reminder else 'ready'):
                    return True
                turn = await self.active_turn()
                params = {'threadId':self.cfg.thread_id,'input':codex_input(event,marker,reminder), 'clientUserMessageId':marker}
                method = 'turn/steer' if turn else 'turn/start'
                if turn:
                    params['expectedTurnId'] = turn
                self.store.update(seq,**{state_field:'inflight',at_field:self.store.clock()})
                try:
                    response = await self.rpc.call(method,params)
                except RpcError as error:
                    self.store.update(seq,**{state_field:'idle','error':f'Codex RPC {error.code}'})
                    if self.retryable_rpc(error):
                        continue
                    self.store.transition(seq,'failed',reason='Codex rejected input',error=f'Codex RPC {error.code}')
                    return True
                except BaseException:
                    # Delivery may have succeeded before a disconnect/cancellation.
                    self.store.update(seq,**{state_field:'uncertain','error':'Codex acknowledgement missing'})
                    raise
                actual_turn = response['turnId'] if method=='turn/steer' else response['turn']['id']
                self.store.delivered_input(seq,actual_turn,reminder)
                log.info('%s seq=%s via %s','Reminded' if reminder else 'Delivered',seq,method)
                return True
            return False

    async def dispatch_once(self):
        row = self.store.dispatch_head()
        if not row or row['status'] != 'ready':
            return False
        return await self.submit(row['seq'])

    async def dispatcher(self):
        delay = 1
        while True:
            try:
                await self.dispatch_once()
                delay = 1
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Codex dispatch waiting (%s)',str(error) if isinstance(error,(RpcError,ConnectionError)) else type(error).__name__)
                async with self.dispatch_lock:
                    self.resumed = False
                    await self.rpc.close()
                await asyncio.sleep(delay);delay = min(30,delay*2)

    async def watchdog_once(self):
        for row in self.store.processing():
            current = self.store.row(row['seq'])
            if current['status']!='processing' or (current['deadline_at'] or float('inf'))>self.store.clock():
                continue
            if current['reminded_at'] is None:
                await self.submit(current['seq'],reminder=True)
            else:
                # A missing reply is local to one input, never a reason to interrupt the shared turn.
                self.store.transition(current['seq'],'failed',reason='no reply within 5 minutes after reminder',error='reply timeout after reminder')

    async def watchdog(self):
        while True:
            try:
                await self.watchdog_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Reminder waiting (%s)',type(error).__name__)
            await asyncio.sleep(1)

    async def reactions_once(self):
        for row in self.store.reactions():
            emoji = row['reaction']
            try:
                await self.bot.set_message_reaction(chat_id=self.cfg.chat_id,message_id=row['message'],
                    reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [],request_timeout=15)
                self.store.reaction_done(row['seq'],emoji)
            except TelegramRetryAfter as error:
                self.store.update(row['seq'],reaction_retry_at=self.store.clock()+error.retry_after+0.2)
                break
            except (TelegramBadRequest,TelegramForbiddenError) as error:
                self.store.reaction_done(row['seq'],emoji,error=type(error).__name__)
                log.warning('Reaction seq=%s unavailable (%s)',row['seq'],type(error).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.store.update(row['seq'],reaction_retry_at=self.store.clock()+5,reaction_error=type(error).__name__)
                break
            await asyncio.sleep(0.1)

    async def reactor(self):
        while True:
            await self.reactions_once()
            await asyncio.sleep(0.5)

    async def outgoing(self):
        while True:
            row = self.store.db.execute("SELECT id,payload FROM outbox WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                try:
                    await self.sender.deliver(row['id'],json.loads(row['payload']))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning('Outgoing delivery waiting (%s)',type(error).__name__)
            await asyncio.sleep(0.5)

    async def ipc(self,reader,writer):
        task = asyncio.current_task();self.ipc_tasks.add(task)
        try:
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(),5))
                key = request['request_id']
                if not isinstance(key,str) or not 1<=len(key)<=160:
                    raise ValueError('Invalid request_id')
                payload = request['payload']
                if payload.get('chat_id')!=self.cfg.chat_id:
                    raise ValueError('Destination chat is not allowed')
                if payload.get('op')=='history':
                    permitted = {'op','chat_id','limit','before','after','message_id','status'}
                    if set(payload)-permitted:
                        raise ValueError('Unsupported history fields')
                    result = self.store.history(self.cfg.chat_id,**{k:v for k,v in payload.items() if k not in {'op','chat_id'}})
                    response = {'ok':True,**result}
                else:
                    ids = await self.sender.deliver('cli-'+key,payload)
                    response = {'ok':True,'message_ids':ids}
            except Exception as error:
                response = {'ok':False,'error':str(error) if isinstance(error,ValueError) else type(error).__name__}
            writer.write(json.dumps(response,ensure_ascii=False).encode()+b'\n')
            await writer.drain()
        finally:
            self.ipc_tasks.discard(task)
            writer.close()
            await writer.wait_closed()


async def run(cfg):
    os.umask(0o077);cfg.prepare()
    lock = open(cfg.state/'bridge.lock','a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close();raise ValueError('Bridge already running')
    info = cfg.token_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
        lock.close();raise ValueError('Token must be an owned regular file with mode 600')
    bot = Bot(cfg.token_file.read_text().strip())
    store,rpc = Store(cfg.state/'queue.sqlite3'),Rpc(cfg)
    bridge = Bridge(cfg,store,bot,rpc)
    server,tasks = None,[]
    try:
        saved = store.get('thread')
        if saved and saved!=cfg.thread_id:
            raise ValueError('Thread changed; use tg-bridge bind while bridge is stopped')
        store.put('thread',cfg.thread_id)
        log.info('Checking Telegram Bot API')
        try:
            webhook = await bot.get_webhook_info(request_timeout=20)
        except asyncio.TimeoutError:
            raise ConnectionError('Telegram: getWebhookInfo timed out after 20s') from None
        if webhook.url:
            raise ValueError('Bot has a webhook; remove it explicitly before local polling')
        if cfg.socket.exists():
            cfg.socket.unlink()
        server = await asyncio.start_unix_server(bridge.ipc,str(cfg.socket),limit=4*1024*1024)
        cfg.socket.chmod(0o600)
        stop = asyncio.Event();loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            loop.add_signal_handler(sig,stop.set)
        tasks = [asyncio.create_task(fn()) for fn in (bridge.poll,bridge.downloader,bridge.dispatcher,bridge.watchdog,bridge.reactor,bridge.outgoing)]
        log.info('Telegram queue ready; Codex connects on demand; thread=%s',cfg.thread_id)
        await stop.wait()
    finally:
        if server:
            server.close();await server.wait_closed()
        pending = tasks+list(bridge.ipc_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        if server:
            cfg.socket.unlink(missing_ok=True)
        await rpc.close();await bot.session.close();store.close();lock.close()
