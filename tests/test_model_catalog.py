"""Live catalog selection, account isolation, cache fallback and API compatibility."""
import asyncio
from pathlib import Path
import tempfile
import types
import unittest

import httpx
from fastapi import FastAPI


def model(identifier):
    return {'id':identifier,'name':identifier,'maxInputTokens':1000000,'maxOutputTokens':64000,
            'supportsToolCall':True,'credits':'x0.11','reasoning':{'defaultEffort':'high','supportedEfforts':['low','high','max']}}


def config(identifier):
    return {'code':0,'data':{'models':[model(identifier),model('old-hidden')],
                            'agents':[{'name':'cli','models':[identifier]}]}}


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        self.assertTrue((Path(__file__).parent.parent/'app/model_catalog.py').exists(),'Live model catalog is missing')
        return __import__('model_catalog')

    def proxy(self):
        client=types.SimpleNamespace(profile=types.SimpleNamespace(key='domestic'),endpoint='https://copilot.tencent.com',
            session={'account':{'uid':'account-a'}},backend='domestic')
        state=types.SimpleNamespace(client=client)
        return types.SimpleNamespace(get_state=lambda:state,
            load_models_from_local_config=lambda backend:[{'id':'legacy-'+backend,'name':'Legacy'}],
            model_to_codex_format=lambda m:{'id':m['id'],'display_name':m.get('name'),'context_window':m.get('max_input')}),state

    async def test_replaces_static_catalog_uses_cli_and_shares_api_output(self):
        m=self.module();proxy,state=self.proxy();calls=[]
        async def fetch(client):calls.append(client);return config('glm-5.3')
        with tempfile.TemporaryDirectory() as tmp:
            catalog=m.ModelCatalog(proxy,Path(tmp),fetch=fetch)
            result=await catalog.get()
            self.assertEqual([r['id'] for r in result['models']],['glm-5.3'])
            self.assertEqual(result['models'][0]['multiplier'],'x0.11')
            self.assertEqual(result['models'][0]['context_window'],1000000)
            self.assertEqual(result['models'][0]['supported_reasoning_levels'][-1]['effort'],'max')
            app=FastAPI()
            @app.get('/v1/models')
            def old():return {'models':[{'id':'stale'}]}
            proxy.app=app;m.install_models_endpoint(proxy,catalog)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
                payload=(await c.get('/v1/models')).json()
                self.assertEqual(payload['models'],payload['data'])
                self.assertEqual(payload['object'],'list')
                self.assertEqual(payload['models'],result['models'])
            self.assertEqual(len(calls),1)

    async def test_refresh_coalescing_and_stale_disk_cache_is_honest(self):
        m=self.module();proxy,state=self.proxy();calls=[];now=[1000];failing=[False]
        async def fetch(client):
            calls.append(client)
            await asyncio.sleep(.01)
            if failing[0]:raise TimeoutError('Do not expose provider data')
            return config('new-model')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);catalog=m.ModelCatalog(proxy,root,fetch=fetch,clock=lambda:now[0])
            results=await asyncio.gather(*(catalog.get() for _ in range(8)))
            self.assertEqual(len(calls),1)
            fetched=results[0]['catalog']['fetched_at']
            now[0]+=12*3600+1;failing[0]=True
            stale=await catalog.get()
            self.assertTrue(stale['catalog']['stale'])
            self.assertEqual(stale['catalog']['fetched_at'],fetched)
            self.assertNotIn('Do not expose provider data',str(stale))
            restored=m.ModelCatalog(proxy,root,fetch=fetch,clock=lambda:now[0])
            disk=await restored.get(force=True)
            self.assertEqual(disk['models'],stale['models'])
            self.assertTrue(disk['catalog']['stale'])

    async def test_account_switch_and_invalid_payload_do_not_reuse_other_account(self):
        m=self.module();proxy,state=self.proxy()
        async def fetch(client):
            if client.profile.key=='global':return {'code':123,'data':{}}
            return config('domestic-only')
        with tempfile.TemporaryDirectory() as tmp:
            catalog=m.ModelCatalog(proxy,Path(tmp),fetch=fetch)
            await catalog.get()
            state.client=types.SimpleNamespace(profile=types.SimpleNamespace(key='global'),endpoint='https://www.codebuddy.ai',
                session={'account':{'uid':'account-b'}},backend='global')
            other=await catalog.get()
            self.assertEqual([r['id'] for r in other['models']],['legacy-global'])
            self.assertTrue(other['catalog']['stale'])
            self.assertEqual(other['catalog']['source'],'package_fallback')


if __name__=='__main__':unittest.main()
