"""Share Desktop's native stdio connection without changing its process ancestry.

The bootstrap execs the signed CLI in place. A forked child brokers JSON-RPC
between Desktop, that CLI, and local WebSocket clients. Native MCP processes
therefore retain their normal Node -> CLI -> Desktop ancestry.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import socket as sockets

from aiohttp import web, WSMsgType


VALUE_OPTIONS = {
    '-c','--config','--enable','--disable','--startup-client-info',
    '--cloud-skills-snapshot','--code-mode-host','--listen','--ws-auth',
    '--ws-token-file','--ws-token-sha256','--ws-shared-secret-file','--ws-issuer',
    '--ws-audience','--ws-max-clock-skew-seconds',
}
SERVER_FLAGS = {'--startup-sign-out','--strict-config','--stdio','--analytics-default-enabled','--managed-daemon'}
ONE_SHOT_FLAGS = {'--print-startup-config','--help','-h','--version','-V'}


def is_server_invocation(arguments):
    """Only wrap a long-running server; preserve CLI startup-policy probes verbatim.

    Unknown shapes fall back to the native CLI. In particular, Desktop must get
    the original stdout/exit status of app-server --print-startup-config.
    """
    index = 0
    found_server = False
    while index < len(arguments):
        argument = arguments[index]
        name = argument.split('=',1)[0]
        if name in ONE_SHOT_FLAGS:
            return False
        if name in VALUE_OPTIONS:
            if '=' not in argument:
                if index+1 >= len(arguments):
                    return False
                if name == '--listen' and arguments[index+1] != 'stdio://':
                    return False
                index += 1
            elif name == '--listen' and argument.split('=',1)[1] != 'stdio://':
                return False
        elif argument.startswith('-c') and argument != '-c':
            pass
        elif argument in SERVER_FLAGS:
            pass
        elif not found_server and argument == 'app-server':
            found_server = True
        else:
            # A subcommand (daemon/proxy/generate-*/help), another CLI command,
            # or an unknown option must be executed normally rather than wrapped.
            return False
        index += 1
    # Desktop also launches isolated app-server helpers (CUA, tool discovery,
    # startup workers). They must keep their own stdio and native lifecycle.
    # Only the main Desktop server carries this observed launch flag.
    return found_server and '--analytics-default-enabled' in arguments


def prepare_socket(socket):
    socket = Path(socket)
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if socket.parent.is_symlink() or socket.parent.stat().st_uid != os.getuid():
        raise ValueError('Desktop socket directory must be owned by this user')
    socket.parent.chmod(0o700)
    if socket.exists() or socket.is_symlink():
        # Never unlink a live socket or another user's file.
        if socket.is_symlink() or not socket.is_socket() or socket.stat().st_uid != os.getuid():
            raise ValueError('Unsafe existing Desktop socket')
        try:
            with sockets.socket(sockets.AF_UNIX) as probe:
                probe.settimeout(1)
                probe.connect(str(socket))
        except (ConnectionRefusedError, FileNotFoundError):
            socket.unlink(missing_ok=True)
        else:
            raise ValueError('Desktop App Server is already listening on this socket')


async def reader_pipe(fd):
    stream = asyncio.StreamReader(limit=32*1024*1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stream), os.fdopen(fd, 'rb', buffering=0))
    return stream, transport


async def writer_pipe(fd):
    loop = asyncio.get_running_loop()
    protocol = asyncio.streams.FlowControlMixin(loop=loop)
    transport, _ = await loop.connect_write_pipe(lambda: protocol, os.fdopen(fd, 'wb', buffering=0))
    return asyncio.StreamWriter(transport, protocol, None, loop)


class Broker:
    def __init__(self, native, desktop):
        self.native, self.desktop = native, desktop
        self.counter = 0
        self.pending = {}
        self.clients = {}
        self.initialize_response = None
        self.desktop_ready = asyncio.Event()

    async def write(self, writer, message):
        writer.write((json.dumps(message, ensure_ascii=False)+'\n').encode())
        await writer.drain()

    async def from_desktop(self, message):
        if 'id' in message and 'method' in message:
            # IDs are connection-local; remap both sources, including initialize.
            await self.request(message, None)
        else:
            await self.write(self.native, message)
            if message.get('method') == 'initialized' and self.initialize_response and 'result' in self.initialize_response:
                self.desktop_ready.set()

    async def request(self, message, client):
        self.counter += 1
        key = f'bridge-mux-{self.counter}'
        self.pending[key] = (client, message['id'], message.get('method'))
        try:
            await self.write(self.native, {**message, 'id': key})
        except BaseException:
            self.pending.pop(key, None)
            raise

    async def from_native(self, message):
        if 'id' in message and 'method' not in message:
            pending = self.pending.pop(message['id'], None)
            if pending is None:
                raise ValueError('Unmatched native RPC response')
            client, original, method = pending
            response = {**message, 'id': original}
            if client is None:
                if method == 'initialize':
                    self.initialize_response = response
                await self.write(self.desktop, response)
                # Desktop versions can start using RPC immediately after this
                # ACK without an additional `initialized` notification.
                if method == 'initialize' and 'result' in response:
                    self.desktop_ready.set()
            elif client in self.clients:
                self.enqueue(client, response)
            return
        # Native approvals, auth/attestation and other server requests belong to
        # the actual Desktop owner. The Telegram client never approves them.
        await self.write(self.desktop, message)
        if 'id' not in message:
            for client, (queue, initialized) in list(self.clients.items()):
                if initialized:
                    self.enqueue(client, message)

    def enqueue(self, client, message):
        queue = self.clients[client][0]
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # Disconnect a slow observer rather than stall Desktop or drop ACKs.
            self.clients.pop(client, None)
            asyncio.create_task(client.close(code=1013, message=b'Slow local client'))

    async def websocket(self, request):
        if request.headers.get('Origin'):
            raise web.HTTPForbidden()
        client = web.WebSocketResponse(max_msg_size=32*1024*1024, heartbeat=20)
        await client.prepare(request)
        queue = asyncio.Queue(maxsize=256)
        self.clients[client] = (queue, False)
        async def send():
            while True:
                await client.send_json(await queue.get())
        sender = asyncio.create_task(send())
        initialized = False
        try:
            async for frame in client:
                if frame.type != WSMsgType.TEXT:
                    continue
                message = json.loads(frame.data)
                method = message.get('method')
                if method == 'initialize' and 'id' in message and not initialized:
                    try:
                        await asyncio.wait_for(self.desktop_ready.wait(), 10)
                    except asyncio.TimeoutError:
                        self.enqueue(client, {'id': message['id'], 'error': {'code': -32000, 'message': 'Desktop has not initialized'}})
                        continue
                    # Desktop's genuine initialize is the only upstream handshake.
                    # Never replace its identity/capabilities with a bridge identity.
                    initialized = True
                    self.enqueue(client, {**self.initialize_response, 'id': message['id']})
                elif method == 'initialized' and initialized:
                    self.clients[client] = (queue, True)
                elif 'id' in message and 'method' in message:
                    if initialized and method != 'initialize':
                        await self.request(message, client)
                    else:
                        self.enqueue(client, {'id': message['id'], 'error': {'code': -32600, 'message': 'Invalid initialization state'}})
                # Bridge notifications and unsolicited replies cannot change the
                # owner session or resolve an approval shown by Desktop.
        finally:
            self.clients.pop(client, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return client


async def serve(native_input, native_output, socket):
    native = await writer_pipe(native_input)
    desktop = await writer_pipe(os.dup(1))
    owner_reader, owner_transport = await reader_pipe(os.dup(0))
    native_reader, native_transport = await reader_pipe(native_output)
    broker = Broker(native, desktop)
    app = web.Application()
    app.router.add_get('/rpc', broker.websocket)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    try:
        await runner.setup()
        await web.UnixSite(runner, str(socket)).start()
        Path(socket).chmod(0o600)
        async def pump(reader, handler):
            while line := await reader.readline():
                await handler(json.loads(line))
        tasks = [asyncio.create_task(pump(owner_reader, broker.from_desktop)),
                 asyncio.create_task(pump(native_reader, broker.from_native)),
                 asyncio.create_task(stop.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in list(broker.clients)), return_exceptions=True)
        await runner.cleanup()
        owner_transport.close()
        native_transport.close()
        native.close()  # Closing the CLI stdin ends its native stdio lifecycle.
        desktop.close()
        Path(socket).unlink(missing_ok=True)


def exec_with_broker(executable, arguments, socket):
    prepare_socket(socket)
    native_read, proxy_write = os.pipe()
    proxy_read, native_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(native_read)
        os.close(native_write)
        exit_code = 0
        try:
            asyncio.run(serve(proxy_write, proxy_read, socket))
        except Exception as error:
            print(f'Telegram Desktop broker: {type(error).__name__}', file=sys.stderr, flush=True)
            exit_code = 1
        finally:
            os._exit(exit_code)
    os.close(proxy_write)
    os.close(proxy_read)
    os.dup2(native_read, 0)
    os.dup2(native_write, 1)
    os.close(native_read)
    os.close(native_write)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('TG_', 'TELEGRAM_'))}
    env.pop('CODEX_CLI_PATH', None)
    env['CODEX_APP_RUNTIME_CLI_PATH'] = executable
    try:
        # Same PID and Desktop parent; Python is no longer an ancestor of MCPs.
        os.execve(executable, [executable, *arguments], env)
    except BaseException:
        os.kill(child, signal.SIGTERM)
        os.waitpid(child, 0)
        raise


def main():
    from .config import Config
    cfg = Config.load()
    arguments = sys.argv[1:]
    if not is_server_invocation(arguments):
        os.execv(cfg.codex, [cfg.codex, *arguments])
    try:
        exec_with_broker(cfg.codex, arguments, cfg.codex_socket)
    except Exception as error:
        print(f'Telegram Desktop adapter: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
