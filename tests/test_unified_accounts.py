import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from unified_accounts import UnifiedAccounts, request_namespace, request_session


class Store:
    def __init__(self):
        self.active_account_id = 'a'
        self.accounts = [dict(id=i, uid=i, nickname=i, backend=b, endpoint='https://' + b)
                         for i, b in [('a', 'domestic'), ('b', 'domestic'), ('c', 'global')]]
        self.clients = {a['id']: SimpleNamespace(name=a['id']) for a in self.accounts}

    def list_accounts(self):
        return copy.deepcopy(self.accounts)

    def get_client(self, account_id):
        return self.clients[account_id]


class Manager:
    def __init__(self):
        self.store = Store()
        self.settings = dict(auto_switch_on_429=True, auto_checkin_enabled=True)
        self.checked = []
        self.checkins = SimpleNamespace(check_in=self.check_in)

    async def check_in(self, account_id, reason='manual'):
        self.checked.append(account_id)
        return dict(status='checked', checked=True)

    def snapshot(self):
        return dict(accounts=self.store.list_accounts(), active_account_id=self.store.active_account_id,
                    settings=dict(self.settings))

    def get_settings(self):
        return dict(self.settings)

    def set_settings(self, **kw):
        self.settings.update(kw)

    def activate(self, account_id):
        self.store.active_account_id = account_id


class UnifiedAccountsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Manager()
        self.calls = []
        self.responses = []

        async def forward(body, protocol, original, state):
            self.calls.append(state.client.name)
            result = self.responses.pop(0) if self.responses else JSONResponse({'ok': True})
            if isinstance(result, Exception):
                raise result
            return result

        state = SimpleNamespace(account_manager=self.manager, account_runtime=self.manager,
                                _client=self.manager.store.get_client('a'))
        state.client = state._client
        self.proxy = SimpleNamespace(proxy_state=state, get_request_state=lambda: copy.copy(state),
                                     _forward_chat_once=forward)
        self.registry = UnifiedAccounts(self.proxy, Path(self.tmp.name))
        self.registry.install()

    def tearDown(self):
        self.tmp.cleanup()

    async def chat(self, session='one', **kwargs):
        token = request_session.set(session)
        try:
            return await self.proxy.forward_chat(dict(model='model', messages=[dict(role='user', content='hello')], **kwargs), 'openai')
        finally:
            request_session.reset(token)

    async def test_bootstrap_flags_and_safe_persistence(self):
        snapshot = await self.registry.snapshot()
        self.assertTrue(all(a['api_enabled'] and a['checkin_enabled'] for a in snapshot['accounts']))
        self.assertFalse(self.manager.settings['auto_checkin_enabled'])
        await self.registry.action(dict(action='flags', id='a', api_enabled=False))
        again = UnifiedAccounts(self.proxy, self.tmp.name)
        self.assertFalse((await again.snapshot())['accounts'][0]['api_enabled'])
        raw = ''.join(p.read_text() for p in Path(self.tmp.name).glob('*.json'))
        self.assertNotIn('hello', raw)
        self.assertNotIn('https://', raw)
        self.assertNotIn('Authorization', raw)

    async def test_checkin_schedule_settings_validate_and_survive_restart(self):
        await self.registry.action(dict(action='settings', checkin_time='10:30', checkin_jitter_minutes=7))
        restored = UnifiedAccounts(self.proxy, self.tmp.name)
        settings = (await restored.snapshot())['settings']
        self.assertEqual(settings['checkin_time'], '10:30')
        self.assertEqual(settings['checkin_jitter_minutes'], 7)
        for update in [{'checkin_time': '25:00'}, {'checkin_time': '9:30'},
                       {'checkin_jitter_minutes': -1}, {'checkin_jitter_minutes': True}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                await self.registry.action(dict(action='settings', **update))

    async def test_checkin_failure_rechecks_real_upstream_status_before_alerting(self):
        from codebuddy_proxy.account_checkin import AccountCheckinService
        reads = 0
        posts = 0
        async def request(client, method, path):
            nonlocal reads, posts
            if path.endswith('/daily-checkin'):
                posts += 1
                return JSONResponse({}, status_code=500)
            reads += 1
            return JSONResponse(dict(code=0, data=dict(active=True, today_checked_in=reads > 1)))
        self.manager.checkins = AccountCheckinService(self.manager, requester=request)
        await self.registry.action(dict(action='flags', id='b', checkin_enabled=False))
        await self.registry.action(dict(action='flags', id='c', checkin_enabled=False))
        result = await self.registry.checkin_all()
        self.assertTrue(result['ok'])
        self.assertEqual(result['accounts'][0]['checkin']['status'], 'checked')
        self.assertEqual(posts, 1)
        self.assertEqual(reads, 2)
        self.assertEqual((await self.registry.checkin('a'))['status'], 'checked')
        self.assertEqual(posts, 1)

    async def test_sticky_round_robin_flags_and_no_global_activation(self):
        await self.chat('one')
        await self.chat('two')
        await self.chat('one')
        self.assertEqual(self.calls, ['a', 'b', 'a'])
        await self.registry.action(dict(action='flags', id='a', api_enabled=False))
        await self.chat('one')
        self.assertEqual(self.calls[-1], 'b')
        self.assertEqual(self.manager.store.active_account_id, 'a')

    async def test_failover_only_before_stream_and_cooldown_by_model(self):
        self.responses = [JSONResponse({}, status_code=429, headers={'Retry-After': '30'}), JSONResponse({})]
        await self.chat()
        self.assertEqual(self.calls, ['a', 'b'])
        await self.chat('another')
        self.assertEqual(self.calls[-1], 'b')
        snapshot = await self.registry.snapshot()
        self.assertGreater(snapshot['accounts'][0]['cooldown_until'], 0)
        self.assertNotIn('c', self.calls)

    async def test_all_accounts_attempted_once_and_disabled_never_retried(self):
        self.responses = [JSONResponse({}, status_code=429)] * 3
        response = await self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.calls, ['a', 'b'])
        await self.registry.action(dict(action='flags', id='b', api_enabled=False))
        with self.assertRaises(HTTPException) as error:
            await self.chat('empty')
        self.assertEqual(error.exception.status_code, 503)

    async def test_auth_failure_retries_but_forbidden_and_network_do_not(self):
        self.responses = [HTTPException(401), JSONResponse({})]
        await self.chat()
        self.assertEqual(self.calls, ['a', 'b'])
        self.responses = [JSONResponse({}, status_code=403)]
        response = await self.chat('next')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.calls), 3)

    async def test_stream_counts_inflight_and_never_replays_partial_output(self):
        async def body():
            yield b'data: first\n\n'
            raise RuntimeError('connection interrupted')

        self.responses = [StreamingResponse(body())]
        response = await self.chat()
        snapshot = await self.registry.snapshot()
        self.assertEqual(snapshot['accounts'][0]['in_flight'], 1)
        chunks = []
        with self.assertRaises(RuntimeError):
            async for chunk in response.body_iterator:
                chunks.append(chunk)
        self.assertEqual(chunks, [b'data: first\n\n'])
        self.assertEqual(self.calls, ['a'])
        self.assertEqual((await self.registry.snapshot())['accounts'][0]['in_flight'], 0)

    async def test_least_inflight_and_fixed_enforce_flags(self):
        await self.registry.action(dict(action='settings', routing_strategy='least_in_flight'))
        async def body():
            yield b'bytes'
        self.responses = [StreamingResponse(body())]
        response = await self.chat('stream')
        await self.chat('new')
        self.assertEqual(self.calls, ['a', 'b'])
        async for _ in response.body_iterator:
            pass
        await self.registry.action(dict(action='settings', routing_strategy='fixed', fixed_account_id='b'))
        await self.chat('fixed')
        self.assertEqual(self.calls[-1], 'b')
        await self.registry.action(dict(action='flags', id='b', api_enabled=False))
        with self.assertRaises(HTTPException):
            await self.chat('fixed-disabled')

    async def test_checkin_flags_independent_and_invalid_updates_atomic(self):
        await self.registry.action(dict(action='flags', id='a', api_enabled=False))
        await self.registry.action(dict(action='flags', id='b', checkin_enabled=False))
        await self.registry.checkin_all()
        self.assertEqual(self.manager.checked, ['a', 'c'])
        with self.assertRaises(ValueError):
            await self.registry.action(dict(action='flags', id='a', api_enabled=True, checkin_enabled='yes'))
        self.assertFalse((await self.registry.snapshot())['accounts'][0]['api_enabled'])

    async def test_dynamic_telemetry_hook_and_no_prompt_saved(self):
        observed = []
        forward = self.proxy._forward_chat_once
        async def hooked(*args):
            observed.append(args[-1].client.name)
            return await forward(*args)
        self.proxy._forward_chat_once = hooked
        await self.chat(None)
        await self.chat(None)
        self.assertEqual(observed, ['a', 'a'])

    async def test_earliest_expiry_uses_remaining_credits_and_scoped_region(self):
        import time
        await self.registry.seed_resources('a', dict(credits_earliest_expiry=time.time() + 3600, credits_earliest_remaining=2))
        await self.registry.seed_resources('b', dict(credits_earliest_expiry=time.time() + 60, credits_earliest_remaining=1))
        await self.registry.seed_resources('c', dict(credits_earliest_expiry=time.time() + 10, credits_earliest_remaining=2))
        await self.registry.action(dict(action='settings', routing_strategy='earliest_expiry'))
        await self.chat('expiry')
        self.assertEqual(self.calls, ['b'])

    async def test_official_summary_uses_total_credits_and_no_group_double_count(self):
        resource = self.registry.project_resources(dict(TotalCredits='12.125',
            Packages=[dict(CycleTotalCapacity='20.5'), dict(CycleTotalCapacity='10')],
            CreditGroups=[dict(CycleTotalCapacity='30.5')], Authorization='secret'))
        self.assertEqual(resource, dict(credits=12.125, credits_total=30.5))

    async def test_upstream_startup_scheduler_replaced_before_state_initialized(self):
        from fastapi import FastAPI
        app = FastAPI()
        async def old_scheduler():
            raise AssertionError('upstream scheduler must never run')
        app.router.add_event_handler('startup', old_scheduler)
        proxy = SimpleNamespace(app=app, proxy_state=None, start_account_checkin=old_scheduler)
        registry = UnifiedAccounts(proxy, Path(self.tmp.name) / 'lazy')
        registry.install()
        self.assertNotIn(old_scheduler, app.router.on_startup)
        proxy.proxy_state = self.proxy.proxy_state
        for callback in app.router.on_startup:
            await callback()
        self.assertTrue((await registry.snapshot())['accounts'][0]['api_enabled'])

    async def test_inference_exception_releases_inflight_and_is_not_replayed(self):
        self.responses = [RuntimeError('transport disconnected')]
        with self.assertRaises(RuntimeError):
            await self.chat()
        self.assertEqual(self.calls, ['a'])
        self.assertEqual((await self.registry.snapshot())['accounts'][0]['in_flight'], 0)

    async def test_resource_refresh_paginates_deduplicates_and_preserves_old_expiry_on_failure(self):
        import time
        soon = time.time() + 3600
        calls = []
        async def request(client, path, body):
            calls.append((path, body))
            if path.endswith('summary'):
                return dict(TotalCredits='19.25', IsPaidUser=True,
                            Packages=[dict(PackageCode='TCACA_code_007', CycleTotalCapacity='40')])
            if path.endswith('paid-packages'):
                return dict(Accounts=[dict(ResourceId='paid', PackageCode='TCACA_code_002',
                    CycleEndTime=soon + 3600, CycleCapacityRemainPrecise='4')], TotalCount=1)
            if body['PageNumber'] == 1:
                return dict(Accounts=[dict(ResourceId='free', PackageCode='TCACA_code_007',
                    DeductionEndTime=soon, CycleCapacityRemainPrecise='3.125')], TotalCount=2)
            return dict(Accounts=[dict(ResourceId='free', PackageCode='TCACA_code_007',
                    DeductionEndTime=soon, CycleCapacityRemainPrecise='3.125'),
                    dict(ResourceId='second', PackageCode='TCACA_code_001', CycleEndTime=soon,
                         CycleCapacityRemainPrecise='2')], TotalCount=2)
        self.registry._resource_request = request
        result = await self.registry.refresh_balance('a')
        self.assertEqual(result['credits'], 19.25)
        self.assertEqual(result['credits_expiring_7d'], 9.125)
        self.assertEqual(result['credits_earliest_remaining'], 9.125)
        self.assertEqual(len(result['expiry_batches']), 2)
        self.assertTrue(result['expiry_known'])
        self.assertEqual(len(calls), 4)
        old_stamp = result['expiry_updated_at']
        async def broken(client, path, body):
            if path.endswith('summary'):
                return dict(TotalCredits='20', IsPaidUser=False, Packages=[])
            raise ValueError('unavailable')
        self.registry._resource_request = broken
        result = await self.registry.refresh_balance('a')
        self.assertFalse(result['expiry_known'])
        snapshot = (await self.registry.snapshot())['accounts'][0]
        self.assertEqual(snapshot['expiry_updated_at'], old_stamp)
        self.assertEqual(snapshot['credits'], 20)
        self.assertEqual(snapshot['credits_earliest_remaining'], 9.125)

    async def test_expiry_rejects_incomplete_pagination_instead_of_inventing_total(self):
        async def incomplete(client, path, body):
            if path.endswith('summary'):
                return dict(TotalCredits='2', IsPaidUser=False, Packages=[])
            return dict(Accounts=[], TotalCount=2)
        self.registry._resource_request = incomplete
        result = await self.registry.refresh_balance('a')
        self.assertFalse(result['expiry_known'])
        self.assertIsNone((await self.registry.snapshot())['accounts'][0]['credits_earliest_expiry'])

    async def test_client_namespaces_isolate_same_first_user_message(self):
        token = request_namespace.set('client-a')
        try:
            await self.chat(None)
            request_namespace.set('client-b')
            await self.chat(None)
            request_namespace.set('client-a')
            await self.chat(None)
        finally:
            request_namespace.reset(token)
        self.assertEqual(self.calls, ['a', 'b', 'a'])

    async def test_reuses_real_upstream_checkin_service_without_global_activation(self):
        from codebuddy_proxy.account_checkin import AccountCheckinService
        checked = set()
        calls = []
        async def request(client, method, path):
            calls.append((client.name, path))
            if path.endswith('daily-checkin'):
                checked.add(client.name)
                return JSONResponse(dict(code=0, data={}))
            return JSONResponse(dict(code=0, data=dict(active=True, today_checked_in=client.name in checked)))
        self.manager.checkins = AccountCheckinService(self.manager, requester=request)
        await self.registry.action(dict(action='flags', id='b', checkin_enabled=False))
        await self.registry.action(dict(action='flags', id='c', checkin_enabled=False))
        result = await self.registry.checkin_all()
        self.assertTrue(result['ok'])
        self.assertEqual(checked, {'a'})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.manager.store.active_account_id, 'a')

    async def test_direct_billing_refreshes_once_and_retains_payload(self):
        import httpx
        from unittest.mock import patch
        original = httpx.AsyncClient
        calls = []
        auth = ['old']
        client = SimpleNamespace(ensure_authenticated=lambda **kwargs: None,
            auth_headers=lambda: {'Authorization': 'Bearer ' + auth[0]},
            refresh=lambda: auth.__setitem__(0, 'fresh') or True,
            build_url=lambda path: 'https://billing.example.com' + path)
        def handler(request):
            calls.append((request.headers['Authorization'], json.loads(request.content)))
            return httpx.Response(401 if len(calls) == 1 else 200, json=dict(code=0, data=dict(TotalCredits='2')))
        transport = httpx.MockTransport(handler)
        with patch('unified_accounts.httpx.AsyncClient', side_effect=lambda **kw: original(transport=transport, **kw)):
            result = await self.registry._resource_request(client, '/billing/meter/get-user-resource-summary', {'Status': [0, 3]})
        self.assertEqual(result['TotalCredits'], '2')
        self.assertEqual(calls, [('Bearer old', {'Status': [0, 3]}), ('Bearer fresh', {'Status': [0, 3]})])

    async def test_missing_saved_session_fails_over_before_any_inference(self):
        get_client = self.manager.store.get_client
        def missing(account_id):
            if account_id == 'a':
                raise FileNotFoundError('credential session missing')
            return get_client(account_id)
        self.manager.store.get_client = missing
        await self.chat('missing-session')
        self.assertEqual(self.calls, ['b'])
        self.assertEqual(self.manager.store.active_account_id, 'a')

    async def test_suffixed_package_codes_use_documented_expiry_family(self):
        import time
        soon, later = time.time() + 100, time.time() + 100000
        result = self.registry._project_expiry([dict(ResourceId='activity',
            PackageCode='TCACA_code_007_opaque', CycleEndTime=later,
            DeductionEndTime=soon, CycleCapacityRemainPrecise='2')], 'domestic')
        self.assertEqual(result['credits_earliest_expiry'], soon)


if __name__ == '__main__':
    unittest.main()
