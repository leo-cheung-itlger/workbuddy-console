"""Upgrade an existing private installation from an extracted clean source tree.

No credentials are embedded. Existing administrator, account and API state is reused.
Run on the server with its virtualenv Python; set WB_PUBLIC_ORIGIN first.
"""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess


def main():
    if os.getuid()!=0:raise SystemExit('Run as root')
    origin=os.environ.get('WB_PUBLIC_ORIGIN','')
    if not origin.startswith('https://') or any(c in origin for c in '\r\n"'):raise ValueError('Set a public HTTPS origin')
    source=Path(__file__).resolve().parent.parent
    state=Path(os.environ.get('WB_STATE_DIR','/var/lib/workbuddy-python'))
    if not (state/'console-auth.json').exists() or not (state/'api-key').exists():
        raise ValueError('Initialize the private state before upgrading')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=Path('/root')/('workbuddy-console-v2-backup-'+stamp);backup.mkdir(mode=0o700)
    target=Path('/opt/workbuddy-console')/'releases'/stamp
    target.mkdir(parents=True,mode=0o755)
    for name in ('app','deploy'):
        shutil.copytree(source/name,target/name)
    for path in target.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    for filename in ('console-auth.json','api-key','mail-alerts-settings.json','unified-accounts.json'):
        path=state/filename
        if path.exists():shutil.copy2(path,backup/filename)
    shutil.copytree(state/'accounts',backup/'accounts')
    for filename in ('console.sqlite3','console-sessions.sqlite3','client-keys.sqlite3'):
        path=state/filename
        if path.exists():
            with sqlite3.connect(path) as old,sqlite3.connect(backup/filename) as new:old.backup(new)
    for name in ('workbuddy-python.service','workbuddy-go-checkin.service','workbuddy-go-checkin.timer',
                 'workbuddy-console-status.service','workbuddy-console-status.timer',
                 'workbuddy-console-watchdog.service','workbuddy-console-watchdog.timer'):
        destination=Path('/etc/systemd/system')/name
        if destination.exists():shutil.copy2(destination,backup/name)
        shutil.copyfile(target/'deploy'/name,destination);destination.chmod(0o644)
    for path in (Path('/etc/caddy/Caddyfile'),Path('/etc/workbuddy-console.env')):
        if path.exists():shutil.copy2(path,backup/path.name)
    (Path('/etc')/'workbuddy-console.env').write_text('WB_PUBLIC_ORIGIN='+origin.rstrip('/')+'\nWB_STATE_DIR='+str(state)+'\n')
    (Path('/etc')/'workbuddy-console.env').chmod(0o600)
    shutil.copyfile(target/'deploy/Caddyfile','/etc/caddy/Caddyfile')
    Path('/etc/caddy/Caddyfile').chmod(0o644)
    subprocess.run(['caddy','validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],check=True)
    # Static paths switch atomically; the previous release remains available for rollback.
    pointer=target.parent.parent/'current';temporary=pointer.with_name('current.next')
    if temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(target,target_is_directory=True);os.replace(temporary,pointer)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','workbuddy-python'],check=True)
    subprocess.run(['systemctl','reload','caddy'],check=True)
    subprocess.run(['systemctl','start','workbuddy-console-status'],check=True)
    print(json.dumps({'upgraded':True,'release':str(target),'backup':str(backup),
                      'old_go_still_running':True,'watchdog_not_enabled_until_validation':True}))


if __name__=='__main__':main()
