from __future__ import annotations

import asyncio
import contextlib
import json
import os

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, UnixConnector, WSMsgType


class RpcError(Exception):
    def __init__(self, error):
        self.code = error.get('code')
        self.details = error
        # Server text may contain prompts or local content; never log it verbatim.
        suffix = ': target thread is owned by a different App Server process' if 'already has an active writer' in error.get('message', '') else ''
        super().__init__(f'App Server RPC error {self.code}{suffix}')


class Rpc:
    """JSON-RPC over WebSocket on the Desktop's existing Unix socket."""
    def __init__(self, config):
        self.cfg = config
        self.session = None
        self.websocket = None
        self.reader = None
        self.initialized = False
        self.pending = {}
        self.counter = 0
        self.notices = asyncio.Queue(maxsize=256)
        self.denied_requests = 0

    async def connect(self):
        if self.initialized and self.websocket and not self.websocket.closed and self.reader and not self.reader.done():
            return
        await self.close()
        if not self.cfg.codex_socket.is_socket():
            raise ConnectionError('Codex: Desktop App Server control socket absent; see README')
        if self.cfg.codex_socket.stat().st_uid != os.getuid():
            raise ConnectionError('Codex: control socket belongs to a different user')
        self.session = ClientSession(connector=UnixConnector(path=str(self.cfg.codex_socket)),
                                     timeout=ClientTimeout(total=15))
        try:
            # Unix listener requires HTTP Upgrade and WebSocket frames, not raw JSONL.
            self.websocket = await asyncio.wait_for(self.session.ws_connect(
                'http://localhost/rpc', max_msg_size=32 * 1024 * 1024, heartbeat=20,
                timeout=ClientWSTimeout(ws_close=5)), timeout=15)
            self.reader = asyncio.create_task(self._read())
            await self.call('initialize', {'clientInfo': {'name': 'telegram_bridge', 'version': '0.1.0'},
                                          'capabilities': {'experimentalApi': True}}, timeout=15)
            await self._write({'method': 'initialized', 'params': {}})
            self.initialized = True
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.TimeoutError):
                raise ConnectionError('Codex: Unix WebSocket handshake timed out after 15s') from None
            raise

    async def _write(self, value):
        if not self.websocket or self.websocket.closed:
            raise ConnectionError('Codex: App Server disconnected')
        await self.websocket.send_json(value, dumps=lambda x: json.dumps(x, ensure_ascii=False))

    async def _read(self):
        try:
            async for frame in self.websocket:
                if frame.type != WSMsgType.TEXT:
                    if frame.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
                        break
                    continue
                msg = json.loads(frame.data)
                if 'id' in msg and 'method' not in msg:
                    future = self.pending.get(msg['id'])
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(RpcError(msg['error']))
                        else:
                            future.set_result(msg.get('result', {}))
                elif 'id' in msg:
                    self.denied_requests += 1
                    await self._write({'id': msg['id'], 'error': {'code': -32601,
                                       'message': 'No interactive approvals in Telegram bridge'}})
                else:
                    if self.notices.full():
                        self.notices.get_nowait()
                    self.notices.put_nowait(msg)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.initialized = False
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError('Codex: App Server connection lost'))

    async def call(self, method, params, timeout=60):
        self.counter += 1
        key = f'tg-{self.counter}'
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self._write({'id': key, 'method': method, 'params': params})
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(f'Codex: {method} timed out after {timeout}s') from None
        finally:
            self.pending.pop(key, None)

    async def pages(self, method, page_size=10, **params):
        cursor = None
        while True:
            page = await self.call(method, {**params, 'cursor': cursor, 'limit': page_size})
            for entry in page['data']:
                yield entry
            cursor = page.get('nextCursor')
            if not cursor:
                break

    async def close(self):
        self.initialized = False
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        if self.session:
            await self.session.close()
            self.session = None
