"""Local lifecycle commands for this workspace's Telegram process only."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import time

from .config import Config


def locked(cfg):
    with (cfg.state/'bridge.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def bridge_pid(cfg):
    if not locked(cfg):
        return None
    result=subprocess.run(['/usr/sbin/lsof','-t',str(cfg.state/'bridge.lock')],
        capture_output=True,text=True,timeout=5)
    for value in set(result.stdout.split()):
        if not value.isdigit():
            continue
        row=subprocess.run(['/bin/ps','-ww','-p',value,'-o','uid=,args='],
            capture_output=True,text=True,timeout=5).stdout.strip().split(None,1)
        if len(row)!=2 or row[0]!=str(os.getuid()):
            continue
        command=row[1]
        if not command.endswith('tg-bridge run'):
            continue
        if str(cfg.root/'.venv/bin/tg-bridge') not in command:
            if '.venv/bin/tg-bridge run' not in command:
                continue
            cwd=subprocess.run(['/usr/sbin/lsof','-a','-p',value,'-d','cwd','-Fn'],
                capture_output=True,text=True,timeout=5).stdout.splitlines()
            if 'n'+str(cfg.root) not in cwd:
                continue
        return int(value)
    if not locked(cfg):
        return None
    raise ValueError('Bridge lock held by an unrecognized process; no process stopped')


def ipc_ready(cfg):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(.2)
            client.connect(str(cfg.socket))
        return True
    except OSError:
        return False


def status(cfg):
    pid=bridge_pid(cfg)
    return {'running':pid is not None,'pid':pid,'ipc_ready':pid is not None and ipc_ready(cfg)}


def start(cfg,timeout=25):
    pid=bridge_pid(cfg)
    if pid is not None:
        return {**status(cfg),'started':False}
    if not (cfg.root/'.venv/bin/tg-bridge').is_file():
        raise ValueError('Install workspace dependencies before starting the bridge')
    log=cfg.state/'bridge.log'
    with log.open('ab') as output:
        log.chmod(0o600)
        process=subprocess.Popen([str(cfg.root/'run.sh')],cwd=cfg.root,
            stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
            start_new_session=True,close_fds=True)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise ValueError('Bridge exited during startup; inspect .state/bridge.log locally')
        if locked(cfg) and ipc_ready(cfg) and bridge_pid(cfg)==process.pid:
            return {'running':True,'pid':process.pid,'ipc_ready':True,'started':True}
        time.sleep(.1)
    raise ValueError('Bridge startup still pending; inspect .state/bridge.log before retrying')


def stop(cfg,timeout=10):
    pid=bridge_pid(cfg)
    if pid is None:
        return {'running':False,'stopped':False}
    try:
        os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if not locked(cfg):
            return {'running':False,'pid':pid,'stopped':True}
        time.sleep(.1)
    raise ValueError('Graceful shutdown pending; no forced kill performed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['status','start','stop','restart'])
    args=parser.parse_args()
    os.umask(0o077)
    try:
        cfg=Config.load();cfg.prepare()
        with (cfg.state/'manage.lock').open('a') as control:
            fcntl.flock(control,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.command=='status':result=status(cfg)
            elif args.command=='stop':result=stop(cfg)
            elif args.command=='start':result=start(cfg)
            else:
                stop(cfg)
                result=start(cfg)
            print(json.dumps({'ok':True,**result}))
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error)}))
        raise SystemExit(1)


if __name__=='__main__':
    main()
