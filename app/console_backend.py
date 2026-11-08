"""Authenticated console adapters over a single account credential owner."""
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from config import API_BASE, STATUS_FILE


def safe_auth_url(value):
    if not isinstance(value,str) or len(value)>4096:return None
    parts=urlsplit(value)
    if parts.scheme!='https' or parts.username or parts.password:return None
    domains=('codebuddy.cn','codebuddy.ai','tencent.com','qq.com','workbuddy.cn','workbuddy.ai')
    return value if parts.hostname and any(parts.hostname==d or parts.hostname.endswith('.'+d) for d in domains) else None


class Backend:
    def __init__(self,root,catalog,accounts,ledger,keys,alerts,benchmarks):
        self.root,self.catalog,self.accounts=Path(root),catalog,accounts
        self.ledger,self.keys,self.alerts,self.benchmarks=ledger,keys,alerts,benchmarks

    def observed_costs(self):
        with self.ledger.lock:
            rows=self.ledger.db.execute("""SELECT model,count(*) samples,sum(credit) credit,
                sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)) tokens FROM attempts
                WHERE credit IS NOT NULL AND complete=1 AND ts>? GROUP BY model""",(time.time()-30*86400,)).fetchall()
        return {r['model']:dict(r) for r in rows}

    async def models(self,force=False):
        data=await self.catalog.get(force=force)
        return {**data,'models':self.benchmarks.enrich(data['models'],self.observed_costs()),
                'benchmark_updated_at':self.benchmarks.snapshot()['updated_at']}

    async def units(self):
        try:
            data=json.loads(STATUS_FILE.read_text())
            if time.time()-data['updated_at']<120:return data['units']
        except (OSError,ValueError,KeyError):pass
        return {}

    async def snapshot(self):
        accounts=await self.accounts.snapshot()
        units=await self.units()
        return {'updated_at':time.time(),'base_url':API_BASE,'accounts':accounts['accounts'],
                'routing':accounts.get('settings',{}),'units':units,'mail':self.alerts.settings(),
                'version':'2.0.0','benchmark_updated_at':self.benchmarks.snapshot()['updated_at']}

    async def action(self,data):
        action=data.get('action')
        manager=self.accounts.proxy.get_state().account_runtime
        if action=='login-start':
            region=data.get('region','domestic')
            if region not in ('domestic','global'):raise ValueError('区域无效')
            job=await asyncio.to_thread(manager.start_login,region)
            return {'id':job.get('id'),'status':job.get('status'),'url':safe_auth_url(job.get('auth_url'))}
        if action in ('login-poll','login-cancel'):
            identifier=data.get('id')
            if not isinstance(identifier,str) or len(identifier)>200:raise ValueError('授权任务无效')
            method=manager.get_login if action=='login-poll' else manager.cancel_login
            job=await asyncio.to_thread(method,identifier)
            return {'id':identifier,'status':job.get('status'),'url':safe_auth_url(job.get('auth_url'))}
        result=await self.accounts.action(data)
        if action=='checkin':
            ok=result.get('status')=='checked'
            await self.alerts.observe_checkin([{'account_id':data.get('id'),'ok':ok}])
            if not ok:raise ValueError('签到结果尚未确认，请检查账户状态')
        return result
