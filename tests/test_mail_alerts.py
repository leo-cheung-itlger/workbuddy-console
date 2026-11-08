"""Passive alert behavior; all delivery uses a local injected sender."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest


class MailAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sent = []
        self.now = 10000

    def manager(self, sender=None):
        from mail_alerts import AlertManager
        return AlertManager(self.root, sender=sender or self.deliver, clock=lambda: self.now)

    def deliver(self, config, message):
        self.sent.append((config, message))

    def enable(self, manager):
        return manager.configure({'enabled': True, 'host': 'smtp.example.com', 'port': 465,
                                  'security': 'ssl', 'username': 'user', 'password': 'secret-password',
                                  'from_email': 'alerts@example.com', 'to_emails': ['admin@example.com']})

    async def observe(self, manager, status=503, **extra):
        await manager.observe({'account_id': 'account-a', 'model': 'model-a', 'status': status,
                               'source': 'upstream', **extra})

    async def test_disabled_default_and_secret_preservation(self):
        manager = self.manager()
        self.assertFalse(manager.settings()['enabled'])
        self.assertEqual((await manager.send_test())['status'], 'disabled')
        safe = self.enable(manager)
        self.assertTrue(safe['password_set'])
        self.assertNotIn('secret-password', json.dumps(safe))
        self.assertNotIn('password', safe)
        manager.configure({'password': '', 'port': 587, 'security': 'starttls'})
        await manager.send_test()
        self.assertEqual(self.sent[0][0]['password'], 'secret-password')
        restarted = self.manager()
        self.assertTrue(restarted.settings()['password_set'])
        restarted.configure({'clear_password': True})
        self.assertFalse(restarted.settings()['password_set'])
        if os.name != 'nt':
            self.assertEqual((self.root / 'mail-alerts-settings.json').stat().st_mode & 0o777, 0o600)

    async def test_header_and_recipient_validation(self):
        manager = self.manager()
        for field, value in [('host', 'smtp.test\r\nX: bad'), ('from_email', 'a@example.com\nBcc: x@test.com'),
                             ('to_emails', ['a@example.com\r\n']), ('to_emails', ['not-an-address']),
                             ('username', 'user\nfoo'), ('security', 'plain')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                manager.configure({field: value})
        manager.configure({'host': '192.0.2.1'})
        self.assertEqual(manager.settings()['host'], '192.0.2.1')

    async def test_failure_threshold_cooldown_recovery_and_restart_dedup(self):
        manager = self.manager()
        self.enable(manager)
        await self.observe(manager)
        await self.observe(manager)
        await manager.flush()
        self.assertEqual(len(self.sent), 0)
        await self.observe(manager)
        await manager.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('api_failure', self.sent[0][1].get_content())
        restarted = self.manager()
        for _ in range(3):
            await self.observe(restarted)
        await restarted.flush()
        self.assertEqual(len(self.sent), 1)
        self.now += 3601
        for _ in range(3):
            await self.observe(restarted)
        await restarted.flush()
        self.assertEqual(len(self.sent), 2)
        await self.observe(restarted, 200)
        await self.observe(restarted, 200)
        await restarted.flush()
        self.assertEqual(len(self.sent), 3)
        self.assertIn('Recovered', self.sent[-1][1]['Subject'])

    async def test_sliding_window_and_rate_limit_do_not_imply_restriction(self):
        manager = self.manager()
        self.enable(manager)
        for _ in range(2):
            await self.observe(manager)
        self.now += 301
        await self.observe(manager)
        for _ in range(3):
            await self.observe(manager, 429)
        for _ in range(3):
            await self.observe(manager, 401, source='incoming')
        await manager.flush()
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0][1].get_content()
        self.assertIn('rate_limit', body)
        self.assertNotIn('restriction', body)
        self.assertNotIn('ban', body.lower())
        for _ in range(3):
            await self.observe(manager, 401)
        await manager.flush()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('upstream_auth', self.sent[-1][1].get_content())

    async def test_explicit_codes_and_sanitized_payloads(self):
        manager = self.manager()
        self.enable(manager)
        for _ in range(3):
            await self.observe(manager, 200, business_code='ACCOUNT_RESTRICTED',
                               raw_body='private-access-token', headers={'Authorization': 'secret'})
        await manager.flush()
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1].as_string()
        self.assertIn('explicit_restriction', text)
        self.assertIn('ACCOUNT_RESTRICTED', text)
        self.assertNotIn('private-access-token', text)
        persisted = (self.root / 'mail-alerts-state.json').read_text()
        self.assertNotIn('private-access-token', persisted)
        self.assertNotIn('Authorization', persisted)

    async def test_checkin_groups_accounts_and_service_recovery(self):
        manager = self.manager()
        self.enable(manager)
        rows = [{'account_id': 'a', 'ok': False}, {'account_id': 'b', 'ok': False}]
        await manager.observe_checkin(rows)
        await manager.observe_checkin(rows)
        await manager.flush()
        self.assertEqual(len(self.sent), 2)
        await manager.observe_checkin([{'account_id': 'a', 'ok': True}])
        await manager.check_services({'python': False})
        await manager.check_services({'python': True})
        await manager.flush()
        self.assertEqual(len(self.sent), 5)

    async def test_smtp_failure_is_safe_and_api_observation_does_not_block(self):
        started, release = threading.Event(), threading.Event()
        def slow_failure(config, message):
            started.set()
            release.wait(2)
            raise RuntimeError('smtp failed secret-password')
        manager = self.manager(slow_failure)
        self.enable(manager)
        for _ in range(3):
            await asyncio.wait_for(self.observe(manager), .3)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        await asyncio.wait_for(self.observe(manager, 200), .3)
        release.set()
        await manager.flush()
        self.assertEqual(manager.settings()['delivery']['status'], 'failed')
        self.assertNotIn('secret-password', json.dumps(manager.settings()))
        result = await manager.send_test()
        self.assertFalse(result['ok'])
        self.assertNotIn('secret-password', json.dumps(result))

    async def test_queue_accepts_recovery_during_delivery_and_flush_drains_it(self):
        started, release = threading.Event(), threading.Event()
        def slow_sender(config, message):
            started.set()
            release.wait(2)
            self.deliver(config, message)
        manager = self.manager(slow_sender)
        self.enable(manager)
        for _ in range(3):
            await self.observe(manager)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        await self.observe(manager, 200)
        release.set()
        await manager.flush()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Recovered', self.sent[-1][1]['Subject'])

    async def test_explicit_test_does_not_duplicate_when_worker_is_busy(self):
        started, release = threading.Event(), threading.Event()
        def slow_sender(config, message):
            started.set()
            release.wait(2)
            self.deliver(config, message)
        manager = self.manager(slow_sender)
        self.enable(manager)
        for _ in range(3):
            await self.observe(manager)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        test_task = asyncio.create_task(manager.send_test())
        await asyncio.sleep(0)
        release.set()
        self.assertTrue((await test_task)['ok'])
        await manager.flush()
        self.assertEqual(len(self.sent), 2)


if __name__ == '__main__':
    unittest.main()
