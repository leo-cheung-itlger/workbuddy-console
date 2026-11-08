import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest

import httpx
from console_telemetry import Ledger, UsageStream, install_observation, request_context


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_database_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            path = Path(folder)/'ledger.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE requests(request_id TEXT PRIMARY KEY,ts REAL,model TEXT,route TEXT,status INTEGER,duration_ms REAL,outcome TEXT);
                CREATE TABLE attempts(id INTEGER PRIMARY KEY,request_id TEXT,ts REAL,model TEXT,account TEXT,protocol TEXT,status INTEGER,duration_ms REAL,prompt_tokens INTEGER,completion_tokens INTEGER,credit REAL,complete INTEGER);''')
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)', ('old',time.time(),'m','/v1/messages',200,1,'completed'))
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (1,'old',time.time(),'m','account-1','anthropic',200,1,8,2,0,1))
            db.commit(); db.close()
            ledger = Ledger(path)
            resources.callback(ledger.db.close)
            row = ledger.requests()['rows'][0]
            self.assertEqual(row['credit'], 0)
            self.assertEqual(row['attempts'][0]['account'], 'account-1')
            self.assertIsNone(row['attempts'][0]['cache_hit_tokens'])
            self.assertFalse(row['attempts'][0]['cache_measured'])
            self.assertIsNone(row['cache_hit_ratio'])
            ledger.db.close()

    async def test_usage_aliases_zero_unknown_and_stream_bytes(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            ledger = Ledger(Path(folder)/'ledger.sqlite3')
            resources.callback(ledger.db.close)
            usages = [({'prompt_tokens':8,'prompt_cache_hit_tokens':0}, 0, 8),
                      ({'prompt_tokens':8,'prompt_tokens_details':{'cached_tokens':6}}, 6, 2),
                      ({'input_tokens':8,'cache_read_input_tokens':4}, 4, None),
                      ({'prompt_tokens':8}, None, None)]
            for n, (usage, hit, miss) in enumerate(usages):
                chunks = [b'data: {"choices":[{"delta":{"content":"private-text"}}]}\n\n',
                          ('data: '+json.dumps({'usage':usage})+'\n\ndata: [DONE]\n\n').encode()]
                class Source(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        for chunk in chunks: yield chunk
                    async def aclose(self): pass
                stream = UsageStream(Source(), ledger, {'request_id':str(n),'status':200})
                self.assertEqual([c async for c in stream], chunks)
                await stream.aclose()
                ledger.add_request({'request_id':str(n),'status':200})
                attempt = next(r for r in ledger.requests()['rows'] if r['request_id']==str(n))['attempts'][0]
                self.assertEqual(attempt['cache_hit_tokens'], hit)
                self.assertEqual(attempt['cache_miss_tokens'], miss)
                self.assertEqual(bool(attempt['cache_measured']), hit is not None)
            summary = ledger.summary()
            self.assertEqual(summary['cache_hit_tokens'], 10)
            self.assertEqual(summary['cache_unknown_attempts'], 1)
            self.assertAlmostEqual(summary['cache_hit_ratio'], 10/28)
            ledger.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.assertNotIn(b'private-text', (Path(folder)/'ledger.sqlite3').read_bytes())
            ledger.db.close()

    async def test_canonical_effort_key_owner_and_sanitized_callback(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            ledger = Ledger(Path(folder)/'ledger.sqlite3')
            resources.callback(ledger.db.close)
            events = []
            proxy = types.SimpleNamespace(httpx=httpx, forward_chat=object())
            route_hook = proxy.forward_chat
            async def upstream(request):
                return httpx.Response(200, content=b'data: {"usage":{"credit":0}}\n\ndata: [DONE]\n\n')
            async def forward(body, protocol, original, state):
                async with proxy.httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                    return await client.post('https://upstream.invalid', json=body)
            proxy._forward_chat_once = forward
            install_observation(proxy, ledger, on_result=events.append, effort_default=lambda model:'medium')
            context = request_context.set({'request_id':'r', 'client_key_id':'client-1', 'client_key_name':'editor'})
            state = types.SimpleNamespace(client='account-1',account_runtime=types.SimpleNamespace(account_id_for_client=lambda c:c))
            try:
                await proxy._forward_chat_once({'model':'m','reasoning_effort':'high','messages':[{'content':'secret prompt'}]}, 'chat', {}, state)
                await proxy._forward_chat_once({'model':'m'}, 'responses', {'reasoning':{'effort':'low'}}, state)
                await proxy._forward_chat_once({'model':'m'}, 'chat', {}, state)
            finally: request_context.reset(context)
            ledger.add_request({'request_id':'r','status':200})
            attempts = ledger.requests()['rows'][0]['attempts']
            self.assertEqual([a['requested_reasoning_effort'] for a in attempts], ['high','low',None])
            self.assertEqual([a['reasoning_effort'] for a in attempts], ['high','low','medium'])
            self.assertEqual([a['reasoning_source'] for a in attempts], ['requested','requested','default'])
            self.assertEqual([a['client_key_id'] for a in attempts], ['client-1']*3)
            self.assertEqual(len(events), 3)
            self.assertNotIn('secret prompt', json.dumps(events))
            self.assertTrue(all(e['connection_status']=='connected' for e in events))
            self.assertIs(proxy.forward_chat, route_hook)
            ledger.db.close()

    async def test_partial_cache_measurements_do_not_invent_ratio(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder)/'ledger.sqlite3')
            try:
                ledger.add_attempt({'request_id':'r','status':200,'cache_hit_tokens':20})
                ledger.add_attempt({'request_id':'r','status':200,'prompt_tokens':100,'cache_hit_tokens':10})
                ledger.add_request({'request_id':'r','status':200})
                self.assertEqual(ledger.summary()['cache_hit_tokens'],30)
                self.assertEqual(ledger.summary()['cache_hit_ratio'],0.1)
                self.assertEqual(ledger.requests()['rows'][0]['cache_hit_ratio'],0.1)
            finally: ledger.db.close()

    async def test_http_business_errors_and_timeouts_are_sanitized(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder)/'ledger.sqlite3')
            events=[]
            proxy=types.SimpleNamespace(httpx=httpx)
            async def upstream(request):
                if request.url.path=='/timeout': raise httpx.ConnectTimeout('private network detail')
                return httpx.Response(429,json={'code':11203,'msg':'private error detail'})
            async def forward(body,protocol,original,state):
                async with proxy.httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                    return await client.post('https://upstream.invalid/'+body['model'])
            proxy._forward_chat_once=forward
            install_observation(proxy,ledger,on_result=events.append)
            context=request_context.set({'request_id':'error'})
            try:
                state=types.SimpleNamespace(client=None)
                response=await proxy._forward_chat_once({'model':'rate-limit'},'chat',{},state)
                self.assertEqual(response.status_code,429)
                with self.assertRaises(httpx.ConnectTimeout):
                    await proxy._forward_chat_once({'model':'timeout'},'chat',{},state)
                self.assertEqual(events[0]['business_code'],'11203')
                self.assertEqual(events[0]['connection_status'],'connected')
                self.assertEqual(events[1]['connection_status'],'timeout')
                self.assertEqual(events[1]['status'],504)
                self.assertNotIn('private',json.dumps(events))
            finally:
                request_context.reset(context)
                ledger.db.close()

    async def test_nested_protocol_usage_and_thinking_metadata(self):
        from console_telemetry import effort_metadata
        self.assertEqual(effort_metadata({'thinking':{'type':'enabled','budget_tokens':1024}})['reasoning_effort'],'thinking:enabled')
        with tempfile.TemporaryDirectory() as folder:
            ledger=Ledger(Path(folder)/'ledger.sqlite3')
            try:
                class Source(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"cache_read_input_tokens":3}}}\n\n'
                        yield b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
                        yield b'data: {"type":"message_stop"}\n\n'
                    async def aclose(self):pass
                stream=UsageStream(Source(),ledger,{'request_id':'anthropic','status':200})
                async for chunk in stream:pass
                ledger.add_request({'request_id':'anthropic','status':200})
                attempt=ledger.requests()['rows'][0]['attempts'][0]
                self.assertEqual(attempt['prompt_tokens'],8)
                self.assertEqual(attempt['completion_tokens'],2)
                self.assertEqual(attempt['cache_hit_ratio'],3/8)
                self.assertTrue(attempt['complete'])
            finally:ledger.db.close()

    async def test_client_cancellation_is_distinct_from_upstream_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Ledger(Path(folder)/'ledger.sqlite3')
            try:
                class Source(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"usage":{"credit":0.25}}\n\n'
                        raise asyncio.CancelledError()
                    async def aclose(self):pass
                stream=UsageStream(Source(),ledger,{'request_id':'cancel','status':200})
                with self.assertRaises(asyncio.CancelledError):
                    async for chunk in stream:pass
                ledger.add_request({'request_id':'cancel','status':200})
                attempt=ledger.requests()['rows'][0]['attempts'][0]
                self.assertEqual(attempt['connection_status'],'cancelled')
                self.assertEqual(attempt['credit'],0.25)
                self.assertFalse(attempt['complete'])
            finally:ledger.db.close()

    async def test_protocol_adapter_closing_after_finish_reason_is_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Ledger(Path(folder)/'ledger.sqlite3')
            try:
                class Source(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"credit":0}}\n\n'
                        yield b'data: [DONE]\n\n'
                    async def aclose(self):pass
                stream=UsageStream(Source(),ledger,{'request_id':'adapter','status':200,'connection_status':'connected'})
                iterator=stream.__aiter__()
                await anext(iterator)
                await iterator.aclose()
                ledger.add_request({'request_id':'adapter','status':200})
                attempt=ledger.requests()['rows'][0]['attempts'][0]
                self.assertTrue(attempt['complete'])
                self.assertEqual(attempt['connection_status'],'connected')
            finally:ledger.db.close()
