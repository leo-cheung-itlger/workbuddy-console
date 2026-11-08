"""Behavior checks for public console authentication and stream accounting."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

import httpx

ROOT = Path(__file__).parent.parent/'app'


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def module(self, name):
        self.assertTrue((ROOT / (name + '.py')).exists(), 'Console implementation is missing')
        return __import__(name)

    async def test_login_session_csrf_and_password_revocation(self):
        module = self.module('console_app')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module.initialize_auth(root, 'admin', 'test-password-123456')
            app = module.create_console(root, None)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=module.ORIGIN) as c:
                self.assertEqual((await c.get('/console/api/me')).status_code, 401)
                bad = await c.post('/console/api/login', json={'username':'admin','password':'wrong'}, headers={'Origin':module.ORIGIN})
                self.assertEqual(bad.status_code, 401)
                good = await c.post('/console/api/login', json={'username':'admin','password':'test-password-123456'}, headers={'Origin':module.ORIGIN})
                self.assertEqual(good.status_code, 200)
                cookie = good.headers['set-cookie'].lower()
                for flag in ['secure', 'httponly', 'samesite=strict', 'path=/console']:
                    self.assertIn(flag, cookie)
                me = (await c.get('/console/api/me')).json()
                denied = await c.post('/console/api/password', json={'current':'test-password-123456','password':'changed-password-7890'}, headers={'Origin':module.ORIGIN})
                self.assertEqual(denied.status_code, 403)
                headers={'Origin':module.ORIGIN,'X-Console-CSRF':me['csrf']}
                forged = await c.post('/console/api/logout', json={}, headers={**headers,'Origin':'https://evil.test'})
                self.assertEqual(forged.status_code, 403)
                changed = await c.post('/console/api/password', json={'current':'test-password-123456','password':'changed-password-7890'}, headers=headers)
                self.assertEqual(changed.status_code, 200)
                self.assertEqual((await c.get('/console/api/me')).status_code, 401)
                text = (root/'console-auth.json').read_text()
                self.assertNotIn('test-password-123456', text)
                self.assertNotIn('changed-password-7890', text)
            app.state.close()

    async def test_bruteforce_limit_and_closed_routes(self):
        module = self.module('console_app')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); module.initialize_auth(root,'admin','test-password-123456')
            app=module.create_console(root,None)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=module.ORIGIN) as c:
                for _ in range(5):
                    self.assertEqual((await c.post('/console/api/login',json={'username':'admin','password':'bad'},headers={'Origin':module.ORIGIN})).status_code,401)
                self.assertEqual((await c.post('/console/api/login',json={'username':'admin','password':'test-password-123456'},headers={'Origin':module.ORIGIN})).status_code,429)
                self.assertEqual((await c.get('/console/../../admin')).status_code,404)
            app.state.close()

    async def test_byte_preservation_zero_unknown_and_attempts(self):
        m=self.module('console_telemetry')
        with tempfile.TemporaryDirectory() as tmp:
            ledger=m.Ledger(Path(tmp)/'console.sqlite3')
            chunks=[b'data: {"choices":[{"delta":{"content":"private message"}}]}\n\n',
                    b'data: {"usage":{"prompt_tokens":16,"completion_tokens":38,"credit":',
                    b'0}}\n\ndata: [DONE]\n\n']
            class Source(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for chunk in chunks:yield chunk
                async def aclose(self):pass
            observer=m.UsageStream(Source(),ledger,{'request_id':'r1','model':'hy3','account':'a1','protocol':'chat','status':200})
            actual=[chunk async for chunk in observer]
            await observer.aclose(); await observer.aclose()
            self.assertEqual(actual,chunks)
            ledger.add_attempt({'request_id':'r1','model':'hy3','account':'a2','status':429})
            ledger.add_request({'request_id':'r1','status':200,'duration_ms':10,'model':'hy3'})
            rows=ledger.requests()['rows']
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['attempt_count'],2)
            self.assertEqual(rows[0]['credit'],0)
            self.assertEqual(rows[0]['unknown_attempts'],1)
            summary=ledger.summary()
            self.assertEqual(summary['requests'],1)
            self.assertEqual(summary['unknown_attempts'],1)
            self.assertNotIn(b'private message',(Path(tmp)/'console.sqlite3').read_bytes())
            ledger.db.close()

    async def test_broken_stream_retains_reported_cost(self):
        m=self.module('console_telemetry')
        with tempfile.TemporaryDirectory() as tmp:
            ledger=m.Ledger(Path(tmp)/'console.sqlite3')
            class Broken(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"usage":{"credit":0.25}}\n\n'
                    raise httpx.ReadError('private upstream text')
                async def aclose(self):pass
            stream=m.UsageStream(Broken(),ledger,{'request_id':'r2','model':'m','account':'a','status':200})
            with self.assertRaises(httpx.ReadError):
                async for chunk in stream:pass
            await stream.aclose()
            ledger.add_request({'request_id':'r2','status':200,'duration_ms':12,'model':'m'})
            row=ledger.requests()['rows'][0]
            self.assertEqual(row['credit'],0.25)
            self.assertEqual(row['outcome'],'incomplete')
            ledger.db.close()

    async def test_upstream_failover_is_correlated_without_rewriting_bytes(self):
        m=self.module('console_telemetry')
        with tempfile.TemporaryDirectory() as tmp:
            ledger=m.Ledger(Path(tmp)/'console.sqlite3')
            payload=b'data: {"usage":{"credit":0.125,"prompt_tokens":5}}\n\ndata: [DONE]\n\n'
            class Source(httpx.AsyncByteStream):
                async def __aiter__(self):yield payload
                async def aclose(self):pass
            async def upstream(request):
                if request.headers['x-account']=='a':return httpx.Response(429,stream=Source())
                return httpx.Response(200,stream=Source())
            proxy=types.SimpleNamespace(httpx=httpx)
            async def forward(body,protocol,original,state):
                async with proxy.httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as c:
                    response=await c.send(c.build_request('POST','https://test.invalid',headers={'x-account':state.client}),stream=True)
                    if response.status_code==429:
                        await response.aclose();return None
                    raw=b''.join([chunk async for chunk in response.aiter_bytes()])
                    await response.aclose();return raw
            proxy._forward_chat_once=forward
            m.install_observation(proxy,ledger)
            token=m.request_context.set({'request_id':'r3'})
            manager=types.SimpleNamespace(account_id_for_client=lambda c:c)
            try:
                for client in ('a','b'):
                    result=await proxy._forward_chat_once({'model':'hy3'},'responses',{},types.SimpleNamespace(client=client,account_runtime=manager))
                self.assertEqual(result,payload)
            finally:m.request_context.reset(token)
            ledger.add_request({'request_id':'r3','status':200,'model':'hy3'})
            row=ledger.requests()['rows'][0]
            self.assertEqual(row['attempt_count'],2)
            self.assertEqual([a['account'] for a in row['attempts']],['a','b'])
            self.assertEqual(row['unknown_attempts'],1)
            self.assertEqual(row['credit'],0.125)
            ledger.db.close()

    async def test_authorization_link_allows_both_official_login_hosts_only(self):
        module=self.module('console_backend')
        for url in ['https://www.workbuddy.cn/login?state=example','https://copilot.tencent.com/login?state=example']:
            self.assertEqual(module.safe_auth_url(url),url)
        for url in ['javascript:alert(1)','https://workbuddy.cn.evil.test/login','https://workbuddy.cn@evil.test/login','http://www.workbuddy.cn/login']:
            self.assertIsNone(module.safe_auth_url(url))


if __name__=='__main__':unittest.main()
