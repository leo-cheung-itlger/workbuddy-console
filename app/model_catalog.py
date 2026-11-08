"""Account-scoped official model catalog shared by the API and console."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

import httpx


class ModelCatalog:
    TTL=12*3600
    RETRY=3600

    def __init__(self,proxy,root,fetch=None,clock=time.time):
        self.proxy,self.root,self.clock=proxy,Path(root),clock
        self.fetch=fetch or self.fetch_official
        self.entries={}
        self.locks={}

    def identity(self,client):
        account=client.session.get('account') or {}
        identity=[client.profile.key,client.endpoint,account.get('uid'),account.get('enterpriseId')]
        return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()

    async def fetch_official(self,client):
        # The config endpoint rejects the package's old Genie-IDE UA (code 12403).
        # This request shape was verified against /v3/config on N1, 2026-11-08.
        await asyncio.to_thread(client.ensure_authenticated,open_browser=False,allow_login=False)
        version='5.9.4' if client.profile.key=='global' else '5.9.3'
        headers={**client.auth_headers(),'User-Agent':f'WorkBuddy/{version} CLI/2.165.0','Accept':'application/json'}
        async with httpx.AsyncClient(timeout=8,trust_env=False) as session:
            async with session.stream('GET',client.build_url('/v3/config'),headers=headers) as response:
                response.raise_for_status()
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>2*1024*1024:raise ValueError('Catalog response too large')
        return json.loads(raw)

    def normalize(self,raw):
        # Reuse the pinned MIT package's protocol conversion; keep live reasoning metadata.
        normalized=self.proxy.normalize_model_format(raw) if hasattr(self.proxy,'normalize_model_format') else {
            'id':raw['id'],'name':raw.get('name'),'max_input':raw.get('maxInputTokens')}
        if not normalized.get('max_input'):normalized['max_input']=raw.get('maxAllowedSize')
        row=self.proxy.model_to_codex_format(normalized)
        row['multiplier']=raw.get('credits') if isinstance(raw.get('credits'),str) else None
        row['credits']=row['multiplier']
        row['max_output_tokens']=raw.get('maxOutputTokens')
        reasoning=raw.get('reasoning')
        if isinstance(reasoning,dict):
            efforts=reasoning.get('supportedEfforts')
            if isinstance(efforts,list):
                row['supported_reasoning_levels']=[{'effort':e,'description':e} for e in efforts if isinstance(e,str)]
            row['default_reasoning_level']=reasoning.get('defaultEffort')
            row['default_reasoning_summary']=reasoning.get('summary','auto')
        return row

    def parse(self,payload):
        if not isinstance(payload,dict) or payload.get('code',0)!=0:raise ValueError('Invalid catalog envelope')
        data=payload.get('data')
        if not isinstance(data,dict) or not isinstance(data.get('models'),list):raise ValueError('Missing model catalog')
        available={m['id']:m for m in data['models'] if isinstance(m,dict)
                   and isinstance(m.get('id'),str) and 0<len(m['id'])<=150}
        agents=data.get('agents')
        if isinstance(agents,dict):agents=list(agents.values())
        cli=next((a for a in agents if isinstance(a,dict) and a.get('name')=='cli'),None) if isinstance(agents,list) else None
        if cli is not None:
            ids=cli.get('models')
            if not isinstance(ids,list):raise ValueError('Invalid CLI selection')
            # Follow the official client selector; don't mix hidden experiments or image/video models.
            selected=[available[i] for i in dict.fromkeys(i for i in ids if isinstance(i,str)) if i in available]
            selection='official_cli'
        else:
            def chat(row):
                identifier=row['id'].lower();tags=row.get('tags') or []
                return (not identifier.startswith(('completion-','codewise-','nes-'))
                        and not any(t in tags for t in ('text-to-image','image-to-image','text-to-video','image-to-video'))
                        and (not row.get('maxOutputTokens') or row['maxOutputTokens']>256))
            selected=[row for row in available.values() if chat(row)]
            selection='official_chat_catalog'
        if not selected:raise ValueError('Empty model selection')
        return [self.normalize(row) for row in selected],selection

    def load(self,key):
        path=self.root/('model-catalog-'+key+'.json')
        try:
            if path.stat().st_size>2*1024*1024:return None
            entry=json.loads(path.read_text(encoding='utf-8'))
            if (entry.get('identity')==key and entry.get('source')=='official_v3'
                    and isinstance(entry.get('models'),list) and entry['models']
                    and isinstance(entry.get('fetched_at'),(int,float))):return entry
        except (OSError,ValueError,TypeError):pass
        return None

    def save(self,key,entry):
        path=self.root/('model-catalog-'+key+'.json');temp=path.with_suffix('.tmp')
        try:
            with open(temp,'w',encoding='utf-8') as stream:
                os.chmod(temp,0o600);json.dump(entry,stream,ensure_ascii=False)
            os.replace(temp,path)
        except OSError:pass  # Live data remains valid even if the optional disk cache cannot be saved.

    async def get(self,force=False):
        client=self.proxy.get_state().client  # Pin before awaiting; a concurrent switch cannot mix catalogs.
        key=self.identity(client)
        lock=self.locks.setdefault(key,asyncio.Lock())
        async with lock:
            entry=self.entries.get(key)
            if entry is None:
                entry=self.load(key)
                if entry is not None:self.entries[key]=entry
            now=self.clock();attempted=entry.get('last_attempt_at',0) if entry else 0
            fresh=bool(entry and not entry.get('failed') and now-entry['fetched_at']<self.TTL)
            throttled=bool(entry and now-attempted<(15 if force else self.RETRY))
            if not (entry and ((fresh and not force) or throttled)):
                try:
                    models,selection=self.parse(await self.fetch(client))
                    entry={'identity':key,'models':models,'selection':selection,'source':'official_v3',
                           'fetched_at':self.clock(),'last_attempt_at':self.clock(),'backend':client.profile.key,'failed':False}
                    self.save(key,entry)
                except Exception:
                    if entry is None:
                        fallback=self.proxy.load_models_from_local_config(client.backend)
                        models=[self.proxy.model_to_codex_format(m) for m in fallback]
                        for model in models:model['multiplier']=None
                        entry={'identity':key,'models':models,'selection':'package_fallback','source':'package_fallback',
                               'fetched_at':None,'backend':client.profile.key}
                    entry={**entry,'last_attempt_at':self.clock(),'failed':True}
                self.entries[key]=entry
            stale=bool(entry.get('failed') or entry['source']!='official_v3')
            metadata={k:entry.get(k) for k in ('source','selection','fetched_at','last_attempt_at','backend')}
            metadata.update(stale=stale,refresh_interval_seconds=self.TTL,
                            cache_age_seconds=max(0,int(self.clock()-entry['fetched_at'])) if entry['fetched_at'] is not None else None,
                            warning='同步失败，正在展示缓存目录' if stale else None)
            return {'models':entry['models'],'catalog':metadata,'updated_at':entry['fetched_at']}


def install_models_endpoint(proxy,catalog):
    async def models():
        result=await catalog.get()
        # Preserve the previous Codex field and add the standard OpenAI list alias.
        return {**result,'object':'list','data':result['models']}
    proxy.app.router.routes[:]=[route for route in proxy.app.router.routes if getattr(route,'path',None)!='/v1/models']
    proxy.app.add_api_route('/v1/models',models,methods=['GET'])
