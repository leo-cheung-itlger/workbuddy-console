"""Independent passive health alerts, including when the API process is stopped."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import urllib.request

from config import STATE,PORT
from mail_alerts import AlertManager


async def main():
    settings=Path(os.environ.get('CREDENTIALS_DIRECTORY','/run/credentials/workbuddy-console-watchdog.service'))/'smtp-settings'
    if not settings.exists():return
    directory=Path(os.environ.get('WB_WATCHDOG_STATE','/var/lib/workbuddy-watchdog'))
    directory.mkdir(mode=0o700,exist_ok=True)
    link=directory/'mail-alerts-settings.json'
    if not link.exists():link.symlink_to(settings)
    manager=AlertManager(directory)
    healthy={}
    for unit in ('workbuddy-python.service','cloudflared-workbuddy.service','caddy.service'):
        result=subprocess.run(['systemctl','is-active',unit],capture_output=True,timeout=5)
        healthy[unit]=result.returncode==0
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{PORT}/health',timeout=8) as response:
            healthy['api-health']=response.status==200
    except Exception:healthy['api-health']=False
    try:
        with opener.open('http://127.0.0.1:18082/ready',timeout=5) as response:
            healthy['public-tunnel']=response.status==200
    except Exception:healthy['public-tunnel']=False
    await manager.check_services(healthy)
    await manager.flush()


if __name__=='__main__':asyncio.run(main())
