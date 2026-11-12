import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError,TelegramRetryAfter,TelegramForbiddenError
from aiogram.methods import SendMessage,SetMessageReaction
from aiogram.types import Message,User

from tg_bridge.config import Config
from tg_bridge.store import Store
from tg_bridge.telegram import chunks,export_bytes,codex_input,download_event,Sender,DownloadRejected
from tg_bridge.bridge import Bridge
from tg_bridge.rpc import RpcError


class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now
    def advance(self,seconds):self.now+=seconds


@pytest.fixture
def cfg(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    root=workspace/'bridge';root.mkdir()
    (root/'TELEGRAM.md').write_text('Transport instructions')
    config=Config(root,workspace,-1001,frozenset({7}),root/'private/token','codex',tmp_path/'codex.sock','thread1')
    config.prepare()
    yield config
    config.socket.parent.rmdir()


@pytest.fixture
def clock():return Clock()


@pytest.fixture
def store(cfg,clock):
    state=Store(cfg.state/'queue.sqlite3',clock)
    yield state
    state.close()


def message(mid=1,**kwargs):
    return Message.model_validate({'message_id':mid,'date':1794437300,
        'chat':{'id':-1001,'type':'supergroup','title':'Test'},
        'from':{'id':7,'is_bot':False,'first_name':'Tester'},**kwargs})


class FakeRpc:
    def __init__(self,cfg):
        self.cfg=cfg;self.websocket=object();self.active=None;self.turns=[];self.requests=[];self.hook=None;self.steer_race=False
    async def connect(self):pass
    async def close(self):pass
    async def call(self,method,params,**kwargs):
        self.requests.append((method,params))
        if method=='thread/read':return {'thread':{'id':self.cfg.thread_id,'cwd':str(self.cfg.workspace),'status':{'type':'active' if self.active else 'idle'}}}
        if method=='thread/resume':return {'thread':{'id':self.cfg.thread_id},'activePermissionProfile':{'id':':workspace'},'approvalPolicy':'on-request'}
        if method=='thread/turns/list':return {'data':self.turns[-1:]}
        if method=='turn/steer' and self.steer_race:
            self.complete();self.steer_race=False
            raise RpcError({'code':-32600,'message':'No active turn'})
        if method=='turn/start':
            if self.active is None:
                self.active='turn'+str(len(self.turns)+1)
                self.turns.append({'id':self.active,'status':'inProgress','items':[]})
        elif method=='turn/steer':assert params['expectedTurnId']==self.active
        else:raise AssertionError(method)
        self.turns[-1]['items'].append({'type':'userMessage','content':params['input']})
        if self.hook:await self.hook()
        return {'turnId':self.active} if method=='turn/steer' else {'turn':{'id':self.active}}
    async def pages(self,*args,**kwargs):
        for turn in reversed(self.turns):yield turn
    def complete(self):
        if self.turns:self.turns[-1]['status']='completed'
        self.active=None
    def deliveries(self):return [(m,p) for m,p in self.requests if m in {'turn/start','turn/steer'}]


def make_bridge(cfg,store,bot=None):
    bot=bot or SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=101)),set_message_reaction=AsyncMock())
    rpc=FakeRpc(cfg);bridge=Bridge(cfg,store,bot,rpc);bridge.sender.pause=AsyncMock()
    return bridge


async def ready(bridge,mid=1,**kwargs):
    bridge.store.ingest(mid,message(mid,**kwargs))
    await bridge.download_once()
    return bridge.store.db.execute('SELECT * FROM inbox WHERE message=?',(mid,)).fetchone()


def test_allowlist(cfg):
    assert cfg.authorized(-1001,7)
    assert not cfg.authorized(-1002,7)


@pytest.mark.parametrize('failure',[None,'forbidden','network'])
async def test_foreign_chat_gets_fixed_notice_without_blocking_allowed_intake(cfg,store,failure):
    from aiogram.types import Update
    notice=SendMessage(chat_id=-2002,text='чат -2002 не разрешен')
    error=(TelegramForbiddenError(method=notice,message='blocked') if failure=='forbidden'
           else TelegramNetworkError(method=notice,message='offline') if failure=='network' else None)
    sent=AsyncMock(side_effect=error)
    next_poll=asyncio.Event()
    batches=0
    async def updates(**kwargs):
        nonlocal batches
        batches+=1
        if batches==1:
            return [
                Update(update_id=1,message=message(1,text='foreign',chat={'id':-2002,'type':'supergroup','title':'Foreign'})),
                Update(update_id=2,message=message(2,text='wrong user',**{'from':{'id':8,'is_bot':False,'first_name':'Other'}})),
                Update(update_id=3,message=message(3,text='bot',chat={'id':-2002,'type':'supergroup','title':'Foreign'},**{'from':{'id':9,'is_bot':True,'first_name':'Bot'}})),
                Update(update_id=4,message=message(4,text='allowed')),
            ]
        next_poll.set()
        await asyncio.Future()
    bridge=make_bridge(cfg,store,SimpleNamespace(get_updates=updates,send_message=sent))
    task=asyncio.create_task(bridge.poll())
    try:
        await asyncio.wait_for(next_poll.wait(),2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    sent.assert_awaited_once()
    assert sent.call_args.kwargs['chat_id']==-2002
    assert sent.call_args.kwargs['text']=='чат -2002 не разрешен'
    assert sent.call_args.kwargs['reply_parameters'].message_id==1
    assert store.get('offset')=='5'
    rows=store.db.execute('SELECT chat,message FROM inbox').fetchall()
    assert [(r['chat'],r['message']) for r in rows]==[(cfg.chat_id,4)]
    assert bridge.rpc.requests==[]
    assert not cfg.authorized(-1001,8)
    assert not cfg.authorized(-1001,None)


@pytest.mark.parametrize('sender_id,from_kind,user_filter',[
    (-1001,'fake',False),(-1001,'absent',False),(-2002,'fake',False),
    (-1001,'fake',True),(-1001,'owner',True),
])
async def test_send_as_chat_intake_preserves_anonymous_identity(cfg,store,sender_id,from_kind,user_filter):
    from dataclasses import replace
    from aiogram.types import Update
    config=replace(cfg,user_ids=cfg.user_ids if user_filter else frozenset())
    sender={'id':sender_id,'type':'supergroup' if sender_id==cfg.chat_id else 'channel',
            'title':'Published author','username':'published_author'}
    incoming=message(1,text='Anonymous request',sender_chat=sender,author_signature='Administrator',
        **{'from':None if from_kind=='absent' else
           {'id':7 if from_kind=='owner' else 1087968824,'is_bot':from_kind!='owner','first_name':'Placeholder'}})
    next_poll=asyncio.Event()
    batches=0
    async def updates(**kwargs):
        nonlocal batches
        batches+=1
        if batches==1:return [Update(update_id=1,message=incoming)]
        next_poll.set();await asyncio.Future()
    bridge=make_bridge(config,store)
    bridge.bot.get_updates=updates
    task=asyncio.create_task(bridge.poll())
    try:await asyncio.wait_for(next_poll.wait(),2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert store.get('offset')=='2'
    if user_filter:
        assert store.head() is None
        return
    queued=store.history(cfg.chat_id)['messages'][0]
    assert queued['user_id'] is None
    assert queued['author']['type']=='chat' and queued['author']['chat_id']==sender_id
    assert queued['sender']=='Published author' and queued['mention']=='@published_author'
    assert queued['author']['signature']=='Administrator'
    await bridge.download_once();await bridge.dispatch_once();await bridge.reactions_once()
    event=json.loads(bridge.rpc.deliveries()[0][1]['input'][0]['text'].split('\n',1)[1])
    assert event['user_id'] is None and event['author']['type']=='chat'
    assert bridge.bot.set_message_reaction.call_args.kwargs['reaction'][0].emoji=='👀'
    assert store.row(1)['reaction_dirty']==0


async def test_foreign_send_as_chat_is_denied_without_codex(cfg,store):
    from aiogram.types import Update
    incoming=message(1,text='Foreign anonymous input',
        chat={'id':-2002,'type':'supergroup','title':'Foreign'},
        sender_chat={'id':-2002,'type':'supergroup','title':'Foreign'},**{'from':None})
    next_poll=asyncio.Event();batches=0
    async def updates(**kwargs):
        nonlocal batches
        batches+=1
        if batches==1:return [Update(update_id=1,message=incoming)]
        next_poll.set();await asyncio.Future()
    bridge=make_bridge(cfg,store);bridge.bot.get_updates=updates
    task=asyncio.create_task(bridge.poll())
    try:await asyncio.wait_for(next_poll.wait(),2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert store.head() is None and bridge.rpc.requests==[]
    assert bridge.bot.send_message.call_args.kwargs['text']=='чат -2002 не разрешен'
    assert store.get('offset')=='2'


def test_durable_queue_offset_and_dedup(store):
    store.ingest(12,message(text='one'));store.ingest(12,message(text='one'));store.ingest(13,message(2,text='two'))
    assert store.get('offset')=='14'
    assert store.db.execute('SELECT count(*) FROM inbox').fetchone()[0]==2
    assert store.head()['message']==1
    assert store.history(-1001)['messages'][0]['text']=='one'
    store.transition(1,'processed');assert store.head()['message']==2


def test_migrate_original_database_without_replaying_running(cfg,clock):
    path=cfg.state/'legacy.sqlite3';db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE inbox(seq INTEGER PRIMARY KEY,chat INTEGER,message INTEGER,raw TEXT,event TEXT,status TEXT,turn TEXT,error TEXT,UNIQUE(chat,message));''')
    event={'attachments':[],'text':'old request'}
    db.execute('INSERT INTO inbox VALUES (1,-1001,1,?,?,?,?,?)',(message(text='old').model_dump_json(),json.dumps(event),'running','old-turn',None))
    db.commit();db.close();migrated=Store(path,clock)
    assert migrated.row(1)['status']=='processing'
    assert migrated.dispatch_head() is None
    assert migrated.history(-1001)['messages'][0]['status']=='processing'
    migrated.close()


def test_utf16_chunks():
    text='Привет🙂'*2200;parts=list(chunks(text))
    assert ''.join(parts)==text
    assert all(len(p.encode('utf-16-le'))//2<=4000 for p in parts)


def test_workspace_relative_export_boundary(cfg,tmp_path):
    path=cfg.output/'report.pdf';path.write_bytes(b'report')
    relative=str(path.relative_to(cfg.workspace))
    assert export_bytes(cfg.workspace,relative)==(b'report','report.pdf')
    external=tmp_path/'outside.pdf';external.write_bytes(b'secret')
    (cfg.output/'link.pdf').symlink_to(external)
    (cfg.output/'hard.pdf').hardlink_to(external)
    (cfg.output/'folder').symlink_to(tmp_path,target_is_directory=True)
    for forbidden in [str(path),'../outside.pdf','output/telegram/link.pdf','output/telegram/hard.pdf',
        'output/telegram/folder/outside.pdf','.env','output/token.txt']:
        with pytest.raises((ValueError,OSError)):export_bytes(cfg.workspace,forbidden)
    private=cfg.root/'notes.txt';private.write_text('private')
    with pytest.raises(ValueError):export_bytes(cfg.workspace,str(private.relative_to(cfg.workspace)),(cfg.root,))


@pytest.mark.parametrize('kind,suffix',[('document','.pdf'),('voice','.ogg'),('audio','.mp3'),('photo','.jpg')])
async def test_media_download_names_and_paths(cfg,kind,suffix):
    value={'file_id':'tg-id','file_unique_id':'unique','file_size':7}
    if kind=='photo':value.update(width=100,height=100);payload={'photo':[value]}
    else:value.update(duration=1,file_name='original'+suffix);payload={kind:value}
    bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='remote/file')))
    async def download(_,destination,**kwargs):destination.write_bytes(b'content')
    bot.download_file=AsyncMock(side_effect=download)
    raw=message(caption='caption',**payload).model_dump_json()
    event=await download_event(bot,cfg,raw);file=event['attachments'][0]
    assert event['text']=='caption' and event['user_id']==7
    assert Path(file['path']).read_bytes()==b'content'
    assert file['relative_path']==f'tg_input/1/1_0{suffix}'
    assert file['message_id']==1
    assert len(codex_input(event,'[marker]'))==(2 if kind=='photo' else 1)
    await download_event(bot,cfg,raw);assert bot.download_file.await_count==1


async def test_download_barrier_then_steer_without_waiting_for_reply(cfg,store):
    gate=asyncio.Event();started=asyncio.Event()
    async def download(_,destination,**kwargs):
        started.set();await gate.wait();destination.write_bytes(b'PDF')
    bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='remote')),
        download_file=AsyncMock(side_effect=download))
    bridge=make_bridge(cfg,store,bot)
    store.ingest(1,message(document={'file_id':'x','file_unique_id':'x','file_name':'doc.pdf'}))
    first=asyncio.create_task(bridge.download_once());await started.wait()
    store.ingest(2,message(2,text='second'))
    assert store.row(1)['status']=='downloading'
    assert await bridge.dispatch_once() is False
    assert bridge.rpc.deliveries()==[]
    gate.set();await first
    await bridge.dispatch_once()
    assert store.row(1)['status']=='processing'
    await bridge.download_once();await bridge.dispatch_once()
    assert store.row(2)['status']=='processing'
    deliveries=bridge.rpc.deliveries()
    assert [m for m,_ in deliveries]==['turn/start','turn/steer']
    assert '[telegram-event:-1001:1]' in deliveries[0][1]['input'][0]['text']
    assert '[telegram-event:-1001:2]' in deliveries[1][1]['input'][0]['text']
    assert deliveries[1][1]['expectedTurnId']==store.row(1)['turn']


async def test_oversized_download_fails_without_incomplete_agent_input(cfg,store):
    bridge=make_bridge(cfg,store)
    await ready(bridge,document={'file_id':'x','file_unique_id':'x','file_size':30*1024*1024})
    assert store.row(1)['status']=='failed'
    assert store.row(1)['reaction']=='👎'
    assert bridge.rpc.deliveries()==[]
    await ready(bridge,2,text='next');await bridge.dispatch_once()
    assert store.row(2)['status']=='processing'


async def test_reply_closes_input_but_standalone_and_turn_completion_do_not(cfg,store,clock):
    bridge=make_bridge(cfg,store);await ready(bridge,text='question');await bridge.dispatch_once()
    await bridge.sender.deliver('announcement',{'op':'text','chat_id':cfg.chat_id,'text':'news'})
    assert store.row(1)['status']=='processing'
    bridge.rpc.complete();await bridge.watchdog_once()
    assert store.row(1)['status']=='processing'
    bridge.bot.send_message.return_value=SimpleNamespace(message_id=101)
    await bridge.sender.deliver('ack',{'op':'text','chat_id':cfg.chat_id,'reply_to':1,'text':'Нужно подумать'})
    assert store.row(1)['status']=='processed'
    clock.advance(1000);await bridge.watchdog_once()
    assert len(bridge.rpc.deliveries())==1
    assert bridge.bot.send_message.call_args.kwargs['reply_parameters'].allow_sending_without_reply is False
    events=store.history(cfg.chat_id)['messages']
    assert events[-1]['reply_to_message_id']==1
    assert events[-1]['text']=='Нужно подумать'


async def test_five_minute_reminder_then_failure_and_late_reply(cfg,store,clock):
    bridge=make_bridge(cfg,store);await ready(bridge,text='long question');await bridge.dispatch_once()
    clock.advance(299);await bridge.watchdog_once();assert len(bridge.rpc.deliveries())==1
    clock.advance(1);await bridge.watchdog_once()
    sent=bridge.rpc.deliveries();assert len(sent)==2
    assert sent[1][0]=='turn/steer'
    assert sent[1][1]['input'][0]['text'].startswith('напоминаю, если забыл\n[telegram-reminder:-1001:1]\n')
    await bridge.watchdog_once();assert len(bridge.rpc.deliveries())==2
    clock.advance(299);await bridge.watchdog_once();assert store.row(1)['status']=='processing'
    clock.advance(1);await bridge.watchdog_once();assert store.row(1)['status']=='failed'
    assert bridge.rpc.active=='turn1'
    assert not any(m=='turn/interrupt' for m,_ in bridge.rpc.requests)
    await bridge.sender.deliver('late',{'op':'text','chat_id':cfg.chat_id,'reply_to':1,'text':'Answer'})
    assert store.row(1)['status']=='processed'


async def test_acknowledgement_before_dispatch_response_is_not_overwritten(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='fast')
    async def reply():await bridge.sender.deliver('fast',{'op':'text','chat_id':cfg.chat_id,'reply_to':1,'text':'ack'})
    bridge.rpc.hook=reply;await bridge.dispatch_once()
    assert store.row(1)['status']=='processed'
    assert store.row(1)['deadline_at'] is None


async def test_finished_turn_race_retries_with_start(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='first');await bridge.dispatch_once()
    await ready(bridge,2,text='second');bridge.rpc.steer_race=True
    await bridge.dispatch_once()
    assert [m for m,_ in bridge.rpc.deliveries()]==['turn/start','turn/steer','turn/start']
    assert store.row(2)['turn']=='turn2'


async def test_reconcile_input_and_uncertain_delivery_barrier(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='one')
    marker=bridge.marker(store.row(1));event=json.loads(store.row(1)['event'])
    bridge.rpc.turns=[{'id':'known','status':'completed','items':[{'type':'userMessage','content':codex_input(event,marker)}]}]
    store.update(1,dispatch_state='uncertain')
    await bridge.dispatch_once()
    assert store.row(1)['status']=='processing' and store.row(1)['turn']=='known'
    assert bridge.rpc.deliveries()==[]
    await ready(bridge,2,text='two');await ready(bridge,3,text='three')
    store.update(2,dispatch_state='uncertain')
    await bridge.dispatch_once()
    assert store.row(2)['status']=='ready' and store.row(3)['status']=='ready'
    assert bridge.rpc.deliveries()==[]


async def test_partial_telegram_success_is_already_a_reply(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='question');await bridge.dispatch_once()
    failure=TelegramNetworkError(method=SendMessage(chat_id=cfg.chat_id,text='x'),message='offline')
    bridge.bot.send_message.side_effect=[SimpleNamespace(message_id=100),failure]
    with pytest.raises(TelegramNetworkError):
        await bridge.sender.deliver('split',{'op':'text','chat_id':cfg.chat_id,'reply_to':1,'text':'x'*5000})
    assert store.row(1)['status']=='processed'
    assert store.outgoing('split')['status']=='uncertain'
    assert len([m for m in store.history(cfg.chat_id)['messages'] if m['direction']=='out'])==1


async def test_sender_dedup_rate_limit_and_forbidden_chat(cfg,store):
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)))
    sender=Sender(cfg,store,bot);sender.pause=AsyncMock()
    payload={'op':'text','chat_id':cfg.chat_id,'text':'hello'}
    assert await sender.deliver('one',payload)==[100]
    assert await sender.deliver('one',payload)==[100]
    assert bot.send_message.await_count==1
    for key,value in [('two',{**payload,'chat_id':123}),('one',{**payload,'text':'different'})]:
        with pytest.raises(ValueError):await sender.deliver(key,value)
    retry=TelegramRetryAfter(method=SendMessage(chat_id=cfg.chat_id,text='hello'),message='rate',retry_after=3)
    bot.send_message.side_effect=[retry,SimpleNamespace(message_id=101)]
    assert await sender.deliver('rate',payload)==[101]
    assert any(call.args==(3.2,) for call in sender.pause.await_args_list)


async def test_reactions_follow_state_and_do_not_destroy_reply_races(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='question');await bridge.dispatch_once()
    async def reply_during_reaction(**kwargs):
        store.transition(1,'processed')
    bridge.bot.set_message_reaction.side_effect=reply_during_reaction
    await bridge.reactions_once()
    assert store.row(1)['reaction_dirty']==1
    assert bridge.bot.set_message_reaction.call_args.kwargs['reaction'][0].emoji=='👀'
    bridge.bot.set_message_reaction.side_effect=None
    await bridge.reactions_once()
    assert bridge.bot.set_message_reaction.call_args.kwargs['reaction'][0].emoji=='👍'
    assert store.row(1)['reaction_dirty']==0


async def test_reaction_permission_failure_does_not_change_lifecycle(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='one');await bridge.dispatch_once()
    bridge.bot.set_message_reaction.side_effect=TelegramForbiddenError(method=SetMessageReaction(chat_id=cfg.chat_id,message_id=1),message='forbidden')
    await bridge.reactions_once()
    assert store.row(1)['status']=='processing'
    assert store.row(1)['reaction_error']=='TelegramForbiddenError'


async def test_history_pagination_and_per_message_status(cfg,store):
    bridge=make_bridge(cfg,store)
    for mid in (1,2,3):await ready(bridge,mid,text=f'input {mid}')
    await bridge.sender.deliver('reply',{'op':'text','chat_id':cfg.chat_id,'reply_to':2,'text':'ack'})
    page=store.history(cfg.chat_id,limit=2)
    assert [m['message_id'] for m in page['messages']]==[3,100]
    older=store.history(cfg.chat_id,limit=2,before=page['next_cursor'])
    assert [m['message_id'] for m in older['messages']]==[1,2]
    assert [m['message_id'] for m in store.history(cfg.chat_id,status='processed')['messages']]==[2]
    assert store.history(cfg.chat_id,message_id=2)['messages'][0]['replied_at'] is not None


async def test_ignore_is_durable_without_reply_or_reminder(cfg,store,clock):
    bridge=make_bridge(cfg,store)
    await ready(bridge,text='conversation between people');await bridge.dispatch_once()
    result=store.ignore(cfg.chat_id,1,'addressed to another participant')
    ignored_at=result['ignored_at']
    assert result['status']=='processed' and result['resolution']=='ignored' and result['changed']
    assert store.row(1)['deadline_at'] is None and store.row(1)['replied_at'] is None
    clock.advance(601)
    again=store.ignore(cfg.chat_id,1,'another reason')
    assert not again['changed'] and again['ignored_at']==ignored_at
    await bridge.watchdog_once();await bridge.reactions_once()
    assert len(bridge.rpc.deliveries())==1
    bridge.bot.send_message.assert_not_awaited()
    assert bridge.bot.set_message_reaction.call_args.kwargs['reaction'][0].emoji=='👍'
    history=store.history(cfg.chat_id)['messages']
    assert len(history)==1 and history[0]['direction']=='in'
    assert history[0]['resolution']=='ignored' and history[0]['replied_at'] is None
    assert history[0]['ignore_reason']=='addressed to another participant'
    assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0


async def test_late_reply_after_ignore_and_ignore_after_reply_preserve_truth(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='first')
    store.ignore(cfg.chat_id,1)
    await bridge.sender.deliver('reply-later',{'op':'text','chat_id':cfg.chat_id,'reply_to':1,'text':'answer'})
    result=store.ignore(cfg.chat_id,1,'do not overwrite delivered reply')
    assert not result['changed'] and result['resolution']=='replied'
    history=store.history(cfg.chat_id,message_id=1)['messages'][0]
    assert history['resolution']=='replied' and history['replied_at'] is not None
    assert history['ignored_at'] is not None


def test_ignore_rejects_unknown_messages_and_invalid_arguments(cfg,store):
    for mid,reason in [(999,''),(True,''),(0,''),('1',''),(1,None),(1,'x'*501)]:
        with pytest.raises(ValueError):store.ignore(cfg.chat_id,mid,reason)
    store.ingest(1,message(text='known'))
    with pytest.raises(ValueError):store.ignore(123,1)
    assert store.row(1)['status']=='queued'


async def test_ignore_before_dispatch_ack_does_not_reopen_processing(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='one')
    store.update(1,dispatch_state='inflight')
    store.ignore(cfg.chat_id,1)
    store.delivered_input(1,'accepted-turn')
    store.delivered_input(1,'reminded-turn',reminder=True)
    assert store.row(1)['status']=='processed' and store.row(1)['resolution']=='ignored'
    assert store.row(1)['deadline_at'] is None


async def test_ignore_failed_input_survives_restart_and_reply_resolution_migrates(cfg,clock):
    path=cfg.state/'ignore-restart.sqlite3';state=Store(path,clock);bridge=make_bridge(cfg,state)
    try:
        await ready(bridge,1,text='ignored');await ready(bridge,2,text='replied')
        state.transition(1,'failed',reason='previous unanswered input')
        state.ignore(cfg.chat_id,1,'not for bot')
        await bridge.sender.deliver('ack',{'op':'text','chat_id':cfg.chat_id,'reply_to':2,'text':'ack'})
        with state.db:state.db.execute('UPDATE inbox SET resolution=NULL WHERE message=2')
        state.close();state=Store(path,clock)
        assert state.row(1)['resolution']=='ignored' and state.row(1)['replied_at'] is None
        assert state.row(2)['resolution']=='replied' and state.row(2)['ignored_at'] is None
    finally:state.close()


async def test_ignore_cli_over_real_ipc_and_destination_guard(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='not addressed to bot')
    server=await asyncio.start_unix_server(bridge.ipc,str(cfg.socket))
    async def command(chat,mid):
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',
            'from tg_bridge.cli import ignore;ignore()',
            '--socket',str(cfg.socket),'--chat-id',str(chat),'--message-id',str(mid),
            '--request-id','ignore-cli-test','--reason','another participant',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        output,_=await asyncio.wait_for(process.communicate(),5)
        return process.returncode,json.loads(output)
    try:
        code,result=await command(123,1)
        assert code==1 and not result['ok'] and store.row(1)['status']=='ready'
        code,result=await command(cfg.chat_id,999)
        assert code==1 and not result['ok']
        code,result=await command(cfg.chat_id,1)
        assert code==0 and result['ok'] and result['resolution']=='ignored'
        code,result=await command(cfg.chat_id,1)
        assert code==0 and not result['changed']
        bridge.bot.send_message.assert_not_awaited()
    finally:
        server.close();await server.wait_closed();cfg.socket.unlink(missing_ok=True)


async def test_restart_preserves_deadline_and_uncertain_send(cfg,clock):
    path=cfg.state/'restart.sqlite3';store=Store(path,clock);bridge=make_bridge(cfg,store)
    await ready(bridge,text='one');await bridge.dispatch_once()
    store.enqueue_send('unknown',{'text':'hello'});store.delivery('unknown','sending')
    original=store.row(1)['deadline_at'];store.close();clock.advance(200)
    store=Store(path,clock)
    assert store.row(1)['status']=='processing' and store.row(1)['deadline_at']==original
    assert store.dispatch_head() is None
    assert store.outgoing('unknown')['status']=='uncertain'
    store.close()


async def test_ipc_reply_history_and_no_agent_status_operation(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='question');await bridge.dispatch_once()
    server=await asyncio.start_unix_server(bridge.ipc,str(cfg.socket))
    try:
        async def cli(function,*arguments):
            p=await asyncio.create_subprocess_exec(sys.executable,'-c',f'from tg_bridge.cli import {function}; {function}()',
                '--socket',str(cfg.socket),'--chat-id',str(cfg.chat_id),*arguments,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,_=await asyncio.wait_for(p.communicate(),10)
            return p.returncode,json.loads(out)
        code,result=await cli('send','--reply-to','1','--request-id','test','--text','ack')
        assert code==0 and result['message_ids']==[100]
        assert store.row(1)['status']=='processed'
        code,result=await cli('history','--message-id','1')
        assert code==0 and result['messages'][0]['status']=='processed'
        reader,writer=await asyncio.open_unix_connection(str(cfg.socket))
        writer.write(json.dumps({'request_id':'forbidden','payload':{'op':'status','chat_id':cfg.chat_id,'message_id':1,'status':'failed'}}).encode()+b'\n')
        await writer.drain();assert not json.loads(await reader.readline())['ok']
        writer.close();await writer.wait_closed()
        assert store.row(1)['status']=='processed'
    finally:
        server.close();await server.wait_closed();cfg.socket.unlink(missing_ok=True)


async def test_attach_preserves_desktop_permissions_and_workspace_roots(cfg,store):
    roots=[str(cfg.workspace),'/existing/desktop/visualizations']
    rpc=SimpleNamespace(connect=AsyncMock(),call=AsyncMock(side_effect=[
        {'thread':{'id':cfg.thread_id,'cwd':str(cfg.workspace),'environments':[{'runtimeWorkspaceRoots':roots}],'status':{'type':'idle'}}},
        {'thread':{'id':cfg.thread_id},'activePermissionProfile':{'id':':workspace'},'approvalPolicy':'on-request'}]))
    bridge=Bridge(cfg,None,None,rpc)
    await bridge.attach()
    assert bridge.resumed is True
    params=rpc.call.call_args_list[1].args[1]
    assert set(params)=={'threadId','excludeTurns','developerInstructions'}
    # The same invariant applies when a delivery starts a new turn.
    bridge=make_bridge(cfg,store)
    await ready(bridge,text='hello');await bridge.dispatch_once()
    params=bridge.rpc.deliveries()[0][1]
    assert not {'permissions','approvalPolicy','config','cwd','runtimeWorkspaceRoots','environments'} & params.keys()


def test_generated_protocol_payloads(cfg):
    import jsonschema
    directory=Path('/tmp/tg-codex-schema/v2')
    if not directory.is_dir():pytest.skip('Generate schema from installed Codex first')
    jsonschema.validate({'threadId':cfg.thread_id,'excludeTurns':True,**cfg.thread_params()},json.loads((directory/'ThreadResumeParams.json').read_text()))
    event={'attachments':[{'type':'photo','path':'/tmp/image.jpg'}],'text':'question'}
    data=codex_input(event,'marker')
    jsonschema.validate({'threadId':cfg.thread_id,'input':data,'clientUserMessageId':'marker'},json.loads((directory/'TurnStartParams.json').read_text()))
    jsonschema.validate({'threadId':cfg.thread_id,'input':data,'clientUserMessageId':'marker','expectedTurnId':'turn1'},json.loads((directory/'TurnSteerParams.json').read_text()))


async def test_steer_ack_lost_reconciles_without_reinjecting(cfg,clock):
    path=cfg.state/'ack-loss.sqlite3';state=Store(path,clock);bridge=make_bridge(cfg,state)
    try:
        await ready(bridge,text='first');await bridge.dispatch_once()
        await ready(bridge,2,text='second')
        async def lost_ack():raise ConnectionError('connection lost after acceptance')
        bridge.rpc.hook=lost_ack
        with pytest.raises(ConnectionError):await bridge.dispatch_once()
        assert state.row(2)['dispatch_state']=='uncertain'
        state.close();state=Store(path,clock);bridge.store=state;bridge.sender.store=state
        bridge.rpc.hook=None
        await bridge.dispatch_once()
        assert state.row(2)['status']=='processing'
        assert len(bridge.rpc.deliveries())==2
    finally:state.close()


async def test_reminder_recovery_keeps_original_five_minute_deadline(cfg,store,clock):
    bridge=make_bridge(cfg,store);await ready(bridge,text='one');await bridge.dispatch_once()
    clock.advance(300)
    async def lost_ack():raise ConnectionError('lost reminder acknowledgement')
    bridge.rpc.hook=lost_ack
    with pytest.raises(ConnectionError):await bridge.watchdog_once()
    original=store.row(1)['reminder_at'];assert store.row(1)['reminder_state']=='uncertain'
    clock.advance(1000);bridge.rpc.hook=None
    await bridge.watchdog_once()
    assert store.row(1)['reminded_at']==original
    assert store.row(1)['deadline_at']==original+300
    await bridge.watchdog_once()
    assert store.row(1)['status']=='failed'
    assert len(bridge.rpc.deliveries())==2


async def test_transient_download_retry_keeps_later_input_behind_barrier(cfg,store,clock):
    bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='remote')))
    attempts=0
    async def download(_,destination,**kwargs):
        nonlocal attempts
        attempts+=1
        destination.write_bytes(b'partial' if attempts==1 else b'complete')
        if attempts==1:raise TimeoutError()
    bot.download_file=AsyncMock(side_effect=download)
    bridge=make_bridge(cfg,store,bot)
    store.ingest(1,message(document={'file_id':'x','file_unique_id':'x','file_name':'doc.pdf'}))
    store.ingest(2,message(2,text='later'))
    await bridge.download_once()
    assert store.row(1)['status']=='downloading'
    assert await bridge.dispatch_once() is False
    assert await bridge.download_once() is False
    clock.advance(1);await bridge.download_once();await bridge.dispatch_once()
    assert store.row(1)['status']=='processing'
    path=Path(json.loads(store.row(1)['event'])['attachments'][0]['path'])
    assert path.read_bytes()==b'complete'
    assert store.row(2)['status']=='queued'


async def test_full_runtime_intake_dispatch_reply_and_history(cfg,monkeypatch):
    from aiogram.types import Update
    from tg_bridge import bridge as module
    cfg.token_file.write_text('test-token');cfg.token_file.chmod(0o600)
    class BotDouble:
        def __init__(self):
            self.sent=False;self.session=SimpleNamespace(close=AsyncMock())
            self.send_message=AsyncMock(return_value=SimpleNamespace(message_id=100))
            self.set_message_reaction=AsyncMock()
        async def get_webhook_info(self,**kwargs):return SimpleNamespace(url='')
        async def get_updates(self,**kwargs):
            if not self.sent:
                self.sent=True
                return [Update(update_id=1,message=message(text='runtime test'))]
            await asyncio.Future()
    bot=BotDouble();rpc=FakeRpc(cfg)
    monkeypatch.setattr(module,'Bot',lambda token:bot)
    monkeypatch.setattr(module,'Rpc',lambda configuration:rpc)
    monkeypatch.setattr(asyncio.get_running_loop(),'add_signal_handler',lambda *args:None)
    async def command(function,*args):
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',f'from tg_bridge.cli import {function}; {function}()',
            '--socket',str(cfg.socket),'--chat-id',str(cfg.chat_id),*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        output,_=await asyncio.wait_for(process.communicate(),10)
        assert process.returncode==0
        return json.loads(output)
    replied=asyncio.Event()
    async def reply():
        await command('send','--reply-to','1','--request-id','runtime-reply','--text','ack')
        replied.set()
    rpc.hook=reply
    runtime=asyncio.create_task(module.run(cfg))
    try:
        await asyncio.wait_for(replied.wait(),10)
        history=await command('history','--message-id','1')
        assert history['messages'][0]['status']=='processed'
        assert bot.send_message.await_count==1
        assert len(rpc.deliveries())==1
    finally:
        runtime.cancel()
        with pytest.raises(asyncio.CancelledError):await runtime
    assert not cfg.socket.exists()
    bot.session.close.assert_awaited_once()


async def test_desktop_starts_turn_between_snapshot_and_start(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='Telegram request')
    original=bridge.active_turn
    async def snapshot_then_desktop_message():
        previous=await original()
        bridge.rpc.active='desktop-turn'
        bridge.rpc.turns.append({'id':'desktop-turn','status':'inProgress','items':[
            {'type':'userMessage','content':[{'type':'text','text':'Direct Desktop request'}]}]})
        return previous
    bridge.active_turn=snapshot_then_desktop_message
    await bridge.dispatch_once()
    assert store.row(1)['status']=='processing'
    assert store.row(1)['turn']=='desktop-turn'
    assert len(bridge.rpc.turns)==1 and len(bridge.rpc.turns[0]['items'])==2
    assert [m for m,_ in bridge.rpc.deliveries()]==['turn/start']


async def test_busy_start_is_rechecked_then_steered(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='Telegram request')
    original=bridge.rpc.call
    rejected=False
    async def call(method,params,**kwargs):
        nonlocal rejected
        if method=='turn/start' and not rejected:
            rejected=True
            bridge.rpc.requests.append((method,params))
            bridge.rpc.active='desktop-turn'
            bridge.rpc.turns.append({'id':'desktop-turn','status':'inProgress','items':[]})
            raise RpcError({'code':-32600,'message':'thread already has an active turn'})
        return await original(method,params,**kwargs)
    bridge.rpc.call=call
    await bridge.dispatch_once()
    assert store.row(1)['status']=='processing' and store.row(1)['turn']=='desktop-turn'
    assert [m for m,_ in bridge.rpc.deliveries()]==['turn/start','turn/steer']
    assert not store.db.execute("SELECT 1 FROM lifecycle WHERE status='failed'").fetchone()


@pytest.mark.parametrize('is_bot,username',[(False,'ivan_petrov'),(True,'tascan_test_bot')])
async def test_reply_contains_original_author_text_and_mention_without_history(cfg,store,is_bot,username):
    bridge=make_bridge(cfg,store)
    parent=message(11,text='Original message',**{'from':{
        'id':55,'is_bot':is_bot,'first_name':'Иван' if not is_bot else 'Codex',
        'last_name':'Петров' if not is_bot else None,'username':username}})
    incoming=message(12,text='Ответ на оригинал',reply_to_message=parent,**{'from':{
        'id':7,'is_bot':False,'first_name':'Анна','username':'anna_test'}})
    store.ingest(12,incoming)
    queued=store.history(cfg.chat_id,message_id=12)['messages'][0]
    assert queued['username']=='anna_test' and queued['mention']=='@anna_test'
    assert queued['reply_to']['text']=='Original message'
    assert queued['reply_to']['author']['user_id']==55
    assert queued['reply_to']['author']['is_bot'] is is_bot
    assert queued['reply_to']['username']==username
    assert queued['reply_to']['mention']=='@'+username
    await bridge.download_once();await bridge.dispatch_once()
    text=bridge.rpc.deliveries()[0][1]['input'][0]['text']
    event=json.loads(text.split('\n',1)[1])
    assert event['reply_to']['text']=='Original message'
    assert event['reply_to']['source']=='telegram_reply'
    assert event['sender']=='Анна' and event['mention']=='@anna_test'
    # Quoting an old parent must not insert it into the agent's processing queue.
    assert store.db.execute('SELECT count(*) FROM inbox').fetchone()[0]==1
    assert not store.history(cfg.chat_id,message_id=11)['messages']


async def test_reply_quotes_caption_without_recursing_or_redownloading(cfg,store):
    bridge=make_bridge(cfg,store)
    ancestor=message(1,text='Do not recursively include this')
    original=message(2,caption='Подпись исходного документа',reply_to_message=ancestor,
        document={'file_id':'old-file','file_unique_id':'old','file_name':'source.pdf'})
    store.ingest(3,message(3,text='Сравните документ',reply_to_message=original))
    await bridge.download_once()
    event=json.loads(store.row(1)['event'])
    parent=event['reply_to']
    assert parent['message_id']==2 and parent['text']=='Подпись исходного документа'
    assert parent['attachments']==[{'type':'document'}]
    assert 'reply_to' not in parent
    assert not (cfg.attachments/'2').exists()
    assert parent['username'] is None and parent['mention'] is None


async def test_confirmed_bot_send_author_and_reply_context_survive_restart(cfg,clock):
    path=cfg.state/'reply-history.sqlite3';state=Store(path,clock)
    bridge=make_bridge(cfg,state)
    bridge.bot.send_message.return_value=SimpleNamespace(message_id=100,
        from_user=User(id=999,is_bot=True,first_name='Codex Bridge',username='tascan_test_bot'))
    try:
        await bridge.sender.deliver('bot-message',{'op':'text','chat_id':cfg.chat_id,'text':'Ответ агента'})
        history=state.history(cfg.chat_id)['messages']
        assert history[0]['direction']=='out' and history[0]['username']=='tascan_test_bot'
        assert history[0]['mention']=='@tascan_test_bot'
        state.ingest(101,message(101,text='уточнение'))
        # Older stored events may only have reply_to_message_id; use local history.
        event=state._raw_event(message(101,text='уточнение').model_dump_json())
        event['reply_to_message_id']=100
        state.downloaded(1,event)
        assert json.loads(state.row(1)['event'])['reply_to']['source']=='local_history'
        state.close();state=Store(path,clock)
        context=state.history(cfg.chat_id,message_id=101)['messages'][0]['reply_to']
        assert context['text']=='Ответ агента' and context['sender']=='Codex Bridge'
        assert context['author']['user_id']==999 and context['mention']=='@tascan_test_bot'
    finally:state.close()


async def test_file_reply_is_preserved_in_history_with_its_relative_path(cfg,store):
    bridge=make_bridge(cfg,store);await ready(bridge,text='Пришлите файл')
    path=cfg.output/'report.pdf';path.write_bytes(b'report')
    relative=str(path.relative_to(cfg.workspace))
    ids=await bridge.sender.deliver('report',{'op':'file','chat_id':cfg.chat_id,'reply_to':1,'file':relative})
    event=store.history(cfg.chat_id,message_id=ids[0])['messages'][0]
    assert event['direction']=='out' and event['reply_to_message_id']==1
    assert event['attachments'][0]['relative_path']==relative
    assert event['reply_to']['text']=='Пришлите файл'
    assert store.row(1)['status']=='processed'
