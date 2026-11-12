import errno
import io
import json
from types import SimpleNamespace

import pytest

from tg_bridge import cli


@pytest.mark.parametrize('failure,expected_code,expected_stage,delivery',[
    ('permission','ipc_permission_denied','connect','not_submitted'),
    ('unavailable','ipc_unavailable','connect','not_submitted'),
    ('lost_ack','ipc_ack_failed','ack','unknown'),
])
def test_ipc_failure_reports_where_delivery_stopped(monkeypatch,capsys,failure,expected_code,expected_stage,delivery):
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,value):pass
        def connect(self,path):
            if failure=='permission':raise PermissionError(errno.EPERM,'Operation not permitted')
            if failure=='unavailable':raise ConnectionRefusedError(errno.ECONNREFUSED,'Connection refused')
        def sendall(self,value):pass
        def makefile(self,mode):return io.BytesIO(b'')
    monkeypatch.setattr(cli.socket,'socket',lambda *args:Socket())
    args=SimpleNamespace(socket='/tmp/test-bridge.sock',request_id='stable-request')
    with pytest.raises(SystemExit) as stopped:
        cli.ipc_request(args,{'op':'text','text':'private message'})
    assert stopped.value.code==1
    output=capsys.readouterr().out
    result=json.loads(output)
    assert result['request_id']=='stable-request'
    assert (result['code'],result['stage'],result['delivery'])==(expected_code,expected_stage,delivery)
    assert 'private message' not in output
    if failure=='permission':
        assert result['errno']==errno.EPERM
        assert 'approval' in result['error']
