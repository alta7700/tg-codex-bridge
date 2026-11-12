import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web

from tg_bridge.config import Config
from tg_bridge.rpc import Rpc, RpcError


def test_desktop_environment_cannot_change_binding(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('TG_CHAT_ID=-1001\nCODEX_THREAD_ID=training-fork\nWORKSPACE=' + str(tmp_path) + '\n')
    monkeypatch.setenv('CODEX_THREAD_ID', 'current-coding-chat')
    assert Config.load(tmp_path).thread_id == 'training-fork'


@pytest.fixture
async def endpoint(tmp_path):
    # Keep Unix socket short even when pytest's temporary directory is long.
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='tg-rpc-', dir='/tmp'))
    socket = root / 'codex.sock'
    seen = []
    async def handler(request):
        assert request.path == '/rpc'
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        initialized = False
        async for frame in ws:
            message = json.loads(frame.data)
            method = message.get('method')
            seen.append(method)
            if method == 'initialize':
                await ws.send_json({'id': message['id'], 'result': {'userAgent': 'test'}})
            elif method == 'initialized':
                initialized = True
                await ws.send_json({'method': 'thread/status/changed', 'params': {'status': {'type': 'idle'}}})
            elif method == 'thread/read':
                assert initialized
                await ws.send_json({'id': message['id'], 'result': {'thread': {'id': 'training-fork'}}})
            elif method == 'test/error':
                await ws.send_json({'id': message['id'], 'error': {'code': -32602, 'message': 'private prompt'}})
            elif method == 'test/drop':
                await ws.close()
            elif method == 'test/timeout':
                pass
        return ws
    app = web.Application()
    app.router.add_get('/rpc', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(socket))
    await site.start()
    cfg = Config(tmp_path, tmp_path, -1001, frozenset(), tmp_path/'token', 'unused', socket, 'training-fork')
    try:
        yield cfg, seen
    finally:
        await runner.cleanup()
        socket.unlink(missing_ok=True)
        root.rmdir()


async def test_real_websocket_rpc_handshake(endpoint):
    cfg, seen = endpoint
    rpc = Rpc(cfg)
    try:
        await rpc.connect()
        assert (await rpc.call('thread/read', {'threadId': cfg.thread_id}))['thread']['id'] == cfg.thread_id
        assert seen[:3] == ['initialize', 'initialized', 'thread/read']
        assert (await asyncio.wait_for(rpc.notices.get(), 1))['method'] == 'thread/status/changed'
        with pytest.raises(RpcError, match='-32602') as error:
            await rpc.call('test/error', {})
        assert 'private prompt' not in str(error.value)
    finally:
        await rpc.close()


async def test_rpc_timeout_names_codex_and_method(endpoint):
    cfg, _ = endpoint
    rpc = Rpc(cfg)
    try:
        await rpc.connect()
        with pytest.raises(ConnectionError, match='Codex: test/timeout timed out'):
            await rpc.call('test/timeout', {}, timeout=0.02)
        assert not rpc.pending
    finally:
        await rpc.close()


async def test_rpc_reconnects_after_disconnect(endpoint):
    cfg, seen = endpoint
    rpc = Rpc(cfg)
    try:
        await rpc.connect()
        with pytest.raises(ConnectionError, match='Codex: App Server connection lost'):
            await rpc.call('test/drop', {}, timeout=2)
        await rpc.connect()
        assert (await rpc.call('thread/read', {'threadId': cfg.thread_id}))['thread']['id'] == cfg.thread_id
        assert seen.count('initialize') == 2
    finally:
        await rpc.close()
