import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import pytest

from aiohttp import ClientSession, UnixConnector

from tg_bridge.desktop_host import is_server_invocation


@pytest.mark.parametrize('arguments',[
    ['app-server','--print-startup-config'],
    ['-c','features.code_mode_host=true','app-server','--print-startup-config','--startup-client-info','{"name":"desktop"}'],
    ['app-server','--help'],
    ['--help','app-server'],
    ['--version'],
    ['app-server','daemon','version'],
    ['app-server','-c','feature=true','generate-json-schema','--out','/tmp/schema'],
    ['app-server','proxy','--sock','/tmp/server.sock'],
    ['app-server','--future-option'],
    ['app-server'],
    ['app-server','--listen','stdio://'],
    ['-c','features.code_mode_host=true','app-server','--listen','stdio://'],
    ['app-server','--analytics-default-enabled','--listen','unix:///tmp/other.sock'],
    ['app-server','--analytics-default-enabled','--listen=off'],
])
def test_one_shot_and_unknown_cli_commands_are_never_wrapped(arguments):
    assert is_server_invocation(arguments) is False


@pytest.mark.parametrize('arguments',[
    ['app-server','--analytics-default-enabled'],
    ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled','-c','plugin.enabled=true'],
    ['app-server','--analytics-default-enabled','--listen=stdio://'],
    ['app-server','--analytics-default-enabled','--startup-client-info','{"name":"desktop"}','--strict-config'],
])
def test_only_long_running_server_invocations_are_wrapped(arguments):
    assert is_server_invocation(arguments) is True


@pytest.mark.parametrize('exit_code',[0,9])
async def test_desktop_startup_policy_probe_preserves_native_stdout_and_exit_status(tmp_path,exit_code):
    # Exercise main(), not just argument classification: this regression broke
    # Desktop's fail-closed organization-policy startup before a server was started.
    fake = tmp_path/'codex'
    fake.write_text('#!'+sys.executable+'\nimport sys\nsys.stdout.write(\'{"requirements":{"mode":"enforced"}}\\n\')\nsys.exit('+str(exit_code)+')\n')
    fake.chmod(0o700)
    code = ('from types import SimpleNamespace;from pathlib import Path;'
            'from tg_bridge.config import Config;'
            'Config.load=classmethod(lambda cls: SimpleNamespace(codex='+repr(str(fake))+'));'
            'from tg_bridge.desktop_host import main;main()')
    process = await asyncio.create_subprocess_exec(sys.executable,'-c',code,'app-server','--print-startup-config',
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr = await asyncio.wait_for(process.communicate(),5)
    assert process.returncode == exit_code
    assert stdout == b'{"requirements":{"mode":"enforced"}}\n'
    assert stderr == b''



async def start_desktop(root, initialized_notification=True):
    endpoint = root / 'app.sock'
    record = root / 'pid.json'
    fake = root / 'codex'
    fake.write_text('#!' + sys.executable + '\n' + r'''
import json,os,sys
from pathlib import Path
Path(os.environ['TEST_HOST_RECORD']).write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid(),'args':sys.argv[1:]}))
count=0
pending=None
def send(value):
    print(json.dumps(value),flush=True)
for line in sys.stdin:
    message=json.loads(line)
    if 'method' not in message:
        if pending is not None:
            send({'id':pending,'result':{'approval':message.get('result')}});pending=None
    elif message['method']=='initialize':
        count+=1
        send({'id':message['id'],'result':{'userAgent':'native-test','owner':message['params']['clientInfo']['name']}})
    elif message['method']=='test/server-request':
        pending=message['id']
        send({'id':'owner-approval','method':'item/commandExecution/requestApproval','params':{}})
    elif message['method']=='test/notice':
        send({'method':'thread/status/changed','params':{'status':{'type':'idle'}}})
        send({'id':message['id'],'result':{}})
    elif 'id' in message:
        send({'id':message['id'],'result':{'server_pid':os.getpid(),'method':message['method'],'initializations':count}})
''')
    fake.chmod(0o700)
    args = ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled',
            '-c','plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true']
    code = ('from tg_bridge.desktop_host import exec_with_broker;exec_with_broker('
            + repr(str(fake))+','+repr(args)+','+repr(str(endpoint))+')')
    proc = await asyncio.create_subprocess_exec(sys.executable,'-c',code,
        env={**os.environ,'TEST_HOST_RECORD':str(record)},stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    proc.stdin.write(json.dumps({'id':1,'method':'initialize','params':{'clientInfo':{'name':'genuine-desktop'},'capabilities':{'experimentalApi':True}}}).encode()+b'\n')
    await proc.stdin.drain()
    hello=json.loads(await asyncio.wait_for(proc.stdout.readline(),5))
    assert hello['id']==1 and hello['result']['owner']=='genuine-desktop'
    if initialized_notification:
        proc.stdin.write(b'{"method":"initialized","params":{}}\n');await proc.stdin.drain()
    return proc,endpoint,record,args


async def stop_desktop(proc,endpoint):
    if proc.returncode is None:
        proc.stdin.close()
        try:await asyncio.wait_for(proc.wait(),5)
        except asyncio.TimeoutError:proc.terminate();await proc.wait()
    for _ in range(100):
        if not endpoint.exists():break
        await asyncio.sleep(.02)
    assert not endpoint.exists(), 'Broker must stop when Desktop/native CLI disconnects'
    assert not (await proc.stderr.read()).strip()


@pytest.mark.parametrize('initialized_notification',[True,False])
async def test_desktop_and_bridge_share_native_pid_without_python_ancestor(tmp_path,initialized_notification):
    root=Path(tempfile.mkdtemp(prefix='tg-host-',dir='/tmp'))
    proc,endpoint,record,args=await start_desktop(root,initialized_notification)
    try:
        saved=json.loads(record.read_text())
        assert saved['pid']==proc.pid  # exec in place, not a child server.
        assert saved['parent']==os.getpid()  # In production this parent is Desktop.
        assert saved['args']==args  # No changes to native stdio/config arguments.
        async with ClientSession(connector=UnixConnector(path=str(endpoint))) as session:
            async with session.ws_connect('http://localhost/rpc') as ws:
                await ws.send_json({'id':1,'method':'initialize','params':{'clientInfo':{'name':'bridge'}}})
                hello=await ws.receive_json()
                assert hello['id']==1 and hello['result']['owner']=='genuine-desktop'
                await ws.send_json({'method':'initialized','params':{}})
                # Same IDs on different clients must keep separate responses.
                proc.stdin.write(b'{"id":7,"method":"owner/echo","params":{}}\n');await proc.stdin.drain()
                await ws.send_json({'id':7,'method':'bridge/echo','params':{}})
                owner=json.loads(await asyncio.wait_for(proc.stdout.readline(),3))
                bridge=await asyncio.wait_for(ws.receive_json(),3)
                assert owner['id']==bridge['id']==7
                assert owner['result']['method']=='owner/echo'
                assert bridge['result']['method']=='bridge/echo'
                assert owner['result']['server_pid']==bridge['result']['server_pid']==proc.pid
                assert bridge['result']['initializations']==1
        assert endpoint.stat().st_mode & 0o777 == 0o600
    finally:
        await stop_desktop(proc,endpoint)
        for path in root.iterdir():path.unlink()
        root.rmdir()


async def test_native_approval_goes_to_desktop_and_notifications_reach_both_clients(tmp_path):
    root=Path(tempfile.mkdtemp(prefix='tg-host-',dir='/tmp'))
    proc,endpoint,_,_=await start_desktop(root)
    try:
        async with ClientSession(connector=UnixConnector(path=str(endpoint))) as session:
            async with session.ws_connect('http://localhost/rpc') as ws:
                await ws.send_json({'id':1,'method':'initialize','params':{}})
                await ws.receive_json();await ws.send_json({'method':'initialized','params':{}})
                await ws.send_json({'id':2,'method':'test/server-request','params':{}})
                request=json.loads(await asyncio.wait_for(proc.stdout.readline(),3))
                assert request['method']=='item/commandExecution/requestApproval'
                # Only Desktop handles the native request; bridge sees the ACK.
                proc.stdin.write(json.dumps({'id':request['id'],'result':{'decision':'decline'}}).encode()+b'\n')
                await proc.stdin.drain()
                reply=await asyncio.wait_for(ws.receive_json(),3)
                assert reply=={'id':2,'result':{'approval':{'decision':'decline'}}}
                await ws.send_json({'id':3,'method':'test/notice','params':{}})
                owner=json.loads(await asyncio.wait_for(proc.stdout.readline(),3))
                bridge=await asyncio.wait_for(ws.receive_json(),3)
                assert owner==bridge and owner['method']=='thread/status/changed'
                assert (await ws.receive_json())['id']==3
    finally:
        await stop_desktop(proc,endpoint)
        for path in root.iterdir():path.unlink()
        root.rmdir()


async def test_broker_exits_when_native_cli_terminates(tmp_path):
    root=Path(tempfile.mkdtemp(prefix='tg-host-',dir='/tmp'))
    proc,endpoint,_,_=await start_desktop(root)
    try:
        proc.terminate();await asyncio.wait_for(proc.wait(),5)
    finally:
        await stop_desktop(proc,endpoint)
        for path in root.iterdir():path.unlink()
        root.rmdir()
