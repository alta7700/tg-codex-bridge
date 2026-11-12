from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import socket
import sys
import uuid

from .config import Config
from .rpc import Rpc, RpcError
from .store import Store, STATUSES


def ipc_request(args,payload):
    # CLI never reads .env or the Bot Token, including the history CLI.
    if not args.socket:
        raise ValueError('--socket is required (see tg-bridge status)')
    stage = 'connect'
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(300);sock.connect(args.socket)
            stage = 'send'
            sock.sendall(json.dumps({'request_id':args.request_id,'payload':payload},ensure_ascii=False).encode()+b'\n')
            stage = 'ack'
            response = sock.makefile('rb').readline(4*1024*1024)
            if not response:
                raise ConnectionError()
            value = json.loads(response)
            if not isinstance(value,dict) or not isinstance(value.get('ok'),bool):
                raise ValueError('Invalid IPC acknowledgement')
            print(json.dumps({'request_id':args.request_id,**value},ensure_ascii=False))
            if not value['ok']:
                raise SystemExit(1)
    except (OSError,ConnectionError,ValueError) as error:
        denied = isinstance(error,PermissionError)
        message = ('IPC permission denied by command sandbox; request normal Desktop approval for this CLI command'
                   if denied else 'IPC connection failed; check bridge is running' if stage=='connect'
                   else 'IPC acknowledgement failed; retry a send with the same request_id')
        print(json.dumps({'ok':False,'request_id':args.request_id,
            'error':message,'code':'ipc_permission_denied' if denied else 'ipc_unavailable' if stage=='connect' else 'ipc_ack_failed',
            'stage':stage,'errno':getattr(error,'errno',None),
            'delivery':'not_submitted' if stage=='connect' else 'unknown'}))
        raise SystemExit(1)


def ipc_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket',required=True)
    parser.add_argument('--chat-id',type=int,required=True)
    parser.add_argument('--request-id',default=str(uuid.uuid4()))
    return parser


def _send(file=False):
    parser = ipc_parser()
    parser.add_argument('--reply-to',type=int)
    parser.add_argument('--file' if file else '--text',required=True)
    args = parser.parse_args()
    ipc_request(args,{'op':'file' if file else 'text','chat_id':args.chat_id,'reply_to':args.reply_to,
        'file' if file else 'text':args.file if file else args.text})


def send():
    _send()


def send_file():
    _send(file=True)


def history():
    parser = ipc_parser()
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--before-message-id',type=int)
    parser.add_argument('--after-message-id',type=int)
    parser.add_argument('--message-id',type=int)
    parser.add_argument('--status',choices=sorted(STATUSES))
    args = parser.parse_args()
    ipc_request(args,{'op':'history','chat_id':args.chat_id,'limit':args.limit,
        **{key:value for key,value in {'before':args.before_message_id,'after':args.after_message_id,
            'message_id':args.message_id,'status':args.status}.items() if value is not None}})


async def doctor(cfg):
    rpc = Rpc(cfg)
    try:
        await rpc.connect()
        thread = (await rpc.call('thread/read',{'threadId':cfg.thread_id}))['thread']
        inventory = await rpc.call('mcpServerStatus/list',{'serverName':'codex_app','detail':'toolsAndAuthOnly'})
        app_tools = next((s for s in inventory.get('data',[]) if s.get('name')=='codex_app'),{})
        tools = app_tools.get('tools') or {}
        print(json.dumps({'connected':True,'thread_id':thread['id'],'status':thread.get('status'),
            'workspace':thread.get('cwd'),'socket':str(cfg.socket),
            'codex_app':{'runtime_status':app_tools.get('runtimeStatus'),
                         'discovery_failed':bool(app_tools.get('toolsError')),
                         'tools_count':len(tools),
                         'has_send_message_to_thread':any(k.endswith('send_message_to_thread') for k in tools)}},ensure_ascii=False))
    finally:
        await rpc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['run','doctor','status','bind','resolve','resolve-send'])
    parser.add_argument('value',nargs='?')
    parser.add_argument('action',nargs='?',choices=['retry','skip'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('aiogram').setLevel(logging.CRITICAL)
    try:
        cfg = Config.load();cfg.prepare()
        if args.command=='run':
            from .bridge import run
            asyncio.run(run(cfg))
        elif args.command=='doctor':
            asyncio.run(doctor(cfg))
        else:
            with open(cfg.state/'bridge.lock','a') as lock:
                if args.command!='status':
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                path = cfg.state/'queue.sqlite3'
                if args.command=='status':
                    import sqlite3
                    if not path.exists():
                        print(json.dumps({'state':'not started','socket':str(cfg.socket)}));return
                    db = sqlite3.connect(f'file:{path}?mode=ro',uri=True)
                    db.row_factory = sqlite3.Row
                    # Status works before and after the additive lifecycle migration.
                    columns = {r['name'] for r in db.execute('PRAGMA table_info(inbox)')}
                    wanted = ['seq','message','status','turn','error','dispatch_state','reminder_state',
                              'processing_at','reminded_at','deadline_at','replied_at','reaction','reaction_error']
                    selected = ','.join(k for k in wanted if k in columns)
                    print(json.dumps({'inbox':[dict(r) for r in db.execute(f'SELECT {selected} FROM inbox ORDER BY seq')],
                        'outbox':[dict(r) for r in db.execute('SELECT id,status,error FROM outbox')],
                        'socket':str(cfg.socket)},ensure_ascii=False))
                    db.close();return
                store = Store(path)
                try:
                    if args.command=='bind':
                        if not args.value:
                            parser.error('bind requires a thread ID')
                        if store.head():
                            raise ValueError('Drain or explicitly skip pending inputs before binding')
                        from dotenv import set_key
                        set_key(cfg.root/'.env','CODEX_THREAD_ID',args.value)
                        store.put('thread',args.value)
                        print('Bound existing thread; history unchanged')
                    elif args.command=='resolve-send':
                        if not args.value or not args.action:
                            parser.error('resolve-send requires outbox ID and retry|skip')
                        row = store.outgoing(args.value)
                        if not row or row['status'] not in {'uncertain','failed'}:
                            raise ValueError('No resolvable outbound delivery')
                        store.delivery(args.value,'queued' if args.action=='retry' else 'sent',[])
                        print('Resolved locally; retry may duplicate an unacknowledged Telegram send')
                    else:
                        if not args.value or not args.action:
                            parser.error('resolve requires inbox sequence number and retry|skip')
                        seq = int(args.value);row = store.row(seq)
                        if not row or (row['status']!='failed' and not any(row[k]=='uncertain' for k in ['dispatch_state','reminder_state'])):
                            raise ValueError('No failed or uncertain input to resolve')
                        if args.action=='skip':
                            store.transition(seq,'failed',reason='explicit local skip',error='skipped locally')
                        elif row['status']=='processing' and row['reminder_state']=='uncertain':
                            store.update(seq,reminder_state='idle',reminder_at=None,deadline_at=store.clock(),error=None)
                        else:
                            store.transition(seq,'ready' if row['event'] else 'queued',reason='explicit local retry',
                                dispatch_state='idle',reminder_state='idle',turn=None,processing_at=None,reminded_at=None,
                                deadline_at=None,dispatch_at=None,reminder_at=None,error=None,download_retry_at=None)
                        print('Resolved locally; inspect status before restart')
                finally:
                    store.close()
    except Exception as error:
        detail = str(error) if isinstance(error,(ValueError,ConnectionError,RpcError)) else type(error).__name__
        print('Bridge: '+detail,file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
