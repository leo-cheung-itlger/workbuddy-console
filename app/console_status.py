"""Publish a credential-free, fixed unit status snapshot for the unprivileged UI."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

UNITS=('caddy.service','workbuddy-python.service','cloudflared-workbuddy.service',
       'workbuddy-go-checkin.timer','workbuddy-go-checkin.service')
FIELDS='Id,ActiveState,Result,ExecMainStatus,ExecMainExitTimestamp,NextElapseUSecRealtime,LastTriggerUSec'


def main():
    result=subprocess.run(['systemctl','show',*UNITS,'--no-pager','--property='+FIELDS],
        capture_output=True,text=True,check=True,timeout=10,env={**os.environ,'TZ':'UTC'})
    units={}
    for block in result.stdout.strip().split('\n\n'):
        fields=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        unit=fields.pop('Id',None)
        if unit not in UNITS:continue
        for field in ('ExecMainExitTimestamp','NextElapseUSecRealtime','LastTriggerUSec'):
            raw=fields.get(field)
            if raw:
                try:fields[field]=datetime.strptime(raw,'%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc).timestamp()
                except ValueError:fields[field]=None
        units[unit]=fields
    root=Path('/run/workbuddy-console-status')
    root.mkdir(mode=0o755,exist_ok=True)
    tmp=root/'status.tmp'
    tmp.write_text(json.dumps({'updated_at':time.time(),'units':units}))
    tmp.chmod(0o644)
    os.replace(tmp,root/'status.json')


if __name__=='__main__':main()
