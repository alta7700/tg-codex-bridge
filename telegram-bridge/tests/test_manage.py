import subprocess
import sys

import pytest

from tg_bridge.config import Config
from tg_bridge import manage


@pytest.fixture
def managed(tmp_path):
    root=tmp_path/'bridge';root.mkdir()
    cfg=Config(root,tmp_path,-1001,frozenset(),root/'private/token','unused',tmp_path/'unused.sock','test')
    cfg.prepare()
    script=root/'.venv/bin/tg-bridge';script.parent.mkdir(parents=True)
    script.write_text('#!'+sys.executable+'\n'+r'''
import asyncio,fcntl,signal
from pathlib import Path
async def main():
    root=Path.cwd()
    lock=(root/'.state/bridge.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    path=Path('''+repr(str(cfg.socket))+r''')
    async def client(reader,writer):
        await reader.read();writer.close();await writer.wait_closed()
    server=await asyncio.start_unix_server(client,str(path))
    stop=asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
    try:await stop.wait()
    finally:server.close();await server.wait_closed();path.unlink(missing_ok=True);lock.close()
asyncio.run(main())
''')
    script.chmod(0o700)
    (root/'run.sh').write_text('#!/bin/sh\nexec "'+str(script)+'" run\n')
    (root/'run.sh').chmod(0o700)
    yield cfg
    if manage.locked(cfg):manage.stop(cfg)
    cfg.socket.unlink(missing_ok=True);cfg.socket.parent.rmdir()


def test_managed_start_is_idempotent_and_restart_preserves_local_data(managed):
    cfg=managed
    marker=cfg.state/'queue.sqlite3';marker.write_bytes(b'existing queue')
    first=manage.start(cfg,timeout=5)
    assert first['started'] and first['ipc_ready']
    second=manage.start(cfg)
    assert not second['started'] and second['pid']==first['pid']
    assert manage.stop(cfg)['stopped']
    restarted=manage.start(cfg,timeout=5)
    assert restarted['pid']!=first['pid'] and marker.read_bytes()==b'existing queue'
    assert manage.stop(cfg)['stopped']
    assert not manage.stop(cfg)['stopped']


def test_manager_refuses_to_signal_unrecognized_lock_holder(managed):
    cfg=managed
    code='import fcntl,time;f=open('+repr(str(cfg.state/'bridge.lock'))+',"a");fcntl.flock(f,fcntl.LOCK_EX);print("ready",flush=True);time.sleep(20)'
    process=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline()==b'ready\n'
        with pytest.raises(ValueError,match='unrecognized process'):
            manage.stop(cfg)
        assert process.poll() is None
    finally:
        process.terminate();process.wait(timeout=5)
