import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from checkin_schedule import CheckinSchedule


class CheckinScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 1, 10, 9, 54, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.settings = {'auto_checkin_enabled': True, 'checkin_time': '10:00', 'checkin_jitter_minutes': 5}
        self.calls = 0
        self.observations = []

        async def snapshot():
            return {'settings': dict(self.settings)}

        async def checkin():
            self.calls += 1
            return {'ok': True, 'accounts': [{'id': 'a', 'checkin': {'status': 'already_done'}}]}

        async def observe(rows):
            self.observations.extend(rows)

        self.accounts = SimpleNamespace(snapshot=snapshot, checkin_all=checkin)
        self.alerts = SimpleNamespace(observe_checkin=observe)

    def schedule(self, pick=None):
        return CheckinSchedule(self.tmp.name, self.accounts, self.alerts,
                               clock=lambda: self.now, choose_offset=pick or (lambda low, high: low))

    async def test_window_persists_across_restart_and_runs_once_under_concurrency(self):
        schedule = self.schedule()
        plan = schedule.snapshot(self.settings)
        self.assertEqual(plan['next_checkin_at'], self.now + 60)
        self.assertTrue((await schedule.run_due())['skipped'])
        picks = []
        def pick(low, high):
            picks.append((low, high))
            return low
        restarted = self.schedule(pick=pick)
        self.assertEqual(restarted.snapshot(self.settings)['next_checkin_at'], plan['next_checkin_at'])
        self.assertEqual(picks, [])
        self.now += 60
        await asyncio.gather(restarted.run_due(), restarted.run_due())
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.observations, [{'account_id': 'a', 'ok': True}])
        self.assertEqual(picks, [(-5, 5)])
        self.assertEqual(restarted.snapshot(self.settings)['last_success'], True)
        again = self.schedule()
        self.assertTrue((await again.run_due())['skipped'])
        self.assertEqual(self.calls, 1)

    async def test_changes_disable_and_midnight_bounds(self):
        schedule = self.schedule(pick=lambda low, high: high)
        first = schedule.snapshot(self.settings)['next_checkin_at']
        self.settings.update(checkin_time='11:30', checkin_jitter_minutes=0)
        changed = schedule.snapshot(self.settings)['next_checkin_at']
        self.assertGreater(changed, first)
        self.settings['auto_checkin_enabled'] = False
        self.now += 86400
        self.assertIsNone(schedule.snapshot(self.settings)['next_checkin_at'])
        self.assertTrue((await schedule.run_due())['skipped'])
        self.assertEqual(self.calls, 0)
        self.settings.update(auto_checkin_enabled=True, checkin_time='23:59', checkin_jitter_minutes=5)
        late = datetime.fromtimestamp(schedule.snapshot(self.settings)['next_checkin_at'], timezone(timedelta(hours=8)))
        self.assertEqual(late.hour, 23)
        self.assertEqual(late.minute, 59)

    async def test_failed_daily_run_not_repeated_each_minute_and_next_day_runs(self):
        async def failed():
            self.calls += 1
            return {'ok': False, 'accounts': [{'id': 'a', 'checkin': {'status': 'error'}}]}
        self.accounts.checkin_all = failed
        self.now += 60
        schedule = self.schedule()
        with patch('checkin_schedule.asyncio.sleep', return_value=None):
            self.assertFalse((await schedule.run_due())['ok'])
            self.assertEqual(self.observations[-1], {'account_id': 'a', 'ok': False})
            self.now += 60
            self.assertTrue((await schedule.run_due())['skipped'])
            self.now += 86400
            await schedule.run_due()
        self.assertEqual(self.calls, 4)
