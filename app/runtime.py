"""Lifecycle workers and a loopback-only daemon endpoint; no paid model probes."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import secrets

from unified_accounts import request_session,request_namespace
from checkin_schedule import run_checkin_batch


def alert_result(row):
    if row.get('connection_status')=='cancelled':return None
    status=0 if row.get('connection_status') in ('timeout','connection_error') else row.get('status')
    if status and 200<=status<300 and not row.get('complete'):status=502
    return {**row,'status':status,'source':'upstream','account_id':row.get('account')}


class SessionBinding:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=scope.get('headers',[])
        explicit=None
        for name in (b'x-session-id',b'x-conversation-id'):
            values=[v for k,v in headers if k.lower()==name]
            if len(values)==1 and 0<len(values[0])<=256:explicit=values[0].decode('utf-8','replace');break
        owner=scope.get('client_key') or {}
        prefix=str(owner.get('id') or owner.get('key_id') or 'legacy')
        namespace=request_namespace.set(prefix)
        token=request_session.set(prefix+':'+explicit if explicit else None)
        try:await self.app(scope,receive,send)
        finally:
            request_session.reset(token)
            request_namespace.reset(namespace)


class InternalTasks:
    def __init__(self,app,root,accounts,alerts,schedule=None):
        self.app,self.accounts,self.alerts=app,accounts,alerts
        self.schedule=schedule
        path=Path(root)/'internal-key'
        if not path.exists():
            with open(path,'w') as stream:
                path.chmod(0o600);stream.write(secrets.token_urlsafe(32))
        self.key=path.read_bytes().strip()

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not scope.get('path','').startswith('/internal/'):
            return await self.app(scope,receive,send)
        values=[v for k,v in scope.get('headers',[]) if k.lower()==b'authorization']
        status=403;body={'error':'Forbidden'}
        try:loopback=ipaddress.ip_address(scope['client'][0]).is_loopback
        except (KeyError,ValueError,TypeError):loopback=False
        forwarded=any(k.lower() in (b'cf-connecting-ip',b'x-forwarded-for',b'origin') for k,v in scope.get('headers',[]))
        if loopback and not forwarded and len(values)==1 and hmac.compare_digest(values[0],b'Bearer '+self.key):
            try:
                if scope.get('method')=='POST' and scope['path']=='/internal/checkin-due' and self.schedule:
                    body=await self.schedule.run_due();status=200
                elif scope.get('method')=='POST' and scope['path']=='/internal/checkin-all':
                    body=await run_checkin_batch(self.accounts,self.alerts);status=200
                else:status=404;body={'error':'Not found'}
            except Exception:body={'ok':False,'error':'签到任务失败'};status=502
        raw=json.dumps(body).encode()
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':raw})


def install_workers(proxy,accounts,backend,alerts):
    tasks=[]
    async def periodic():
        await asyncio.sleep(3)
        snapshot=await accounts.snapshot()
        for account in snapshot['accounts']:
            try:await accounts.refresh_balance(account['id'])
            except Exception:pass
        while True:
            # The independent systemd watchdog owns service checks, so it can alert
            # even when this process is down. Resume only the app's metadata outbox here.
            alerts._start_worker()
            await asyncio.sleep(60)
    @proxy.app.on_event('startup')
    async def startup():tasks.append(asyncio.create_task(periodic()))
    @proxy.app.on_event('shutdown')
    async def shutdown():
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await alerts.flush()
