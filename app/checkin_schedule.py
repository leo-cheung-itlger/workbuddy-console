"""Persist one randomized daily plan, dispatched by the minute timer."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import time

from checkin_result import checkin_succeeded
from mail_alerts import _atomic_json, _load


BEIJING = timezone(timedelta(hours=8))


async def run_checkin_batch(accounts, alerts):
    for attempt in range(2):
        result = await accounts.checkin_all()
        rows = result.get('accounts', [])
        success = all(checkin_succeeded(row.get('checkin')) for row in rows) if rows else result.get('ok') is True
        if success or attempt == 1:
            break
        await asyncio.sleep(5)
    result['ok'] = success
    await alerts.observe_checkin([
        {'account_id': row.get('id'), 'ok': checkin_succeeded(row.get('checkin'))} for row in rows
    ])
    return result


class CheckinSchedule:
    def __init__(self, root, accounts, alerts, clock=None, choose_offset=None):
        self.path = Path(root) / 'checkin-schedule.json'
        self.accounts = accounts
        self.alerts = alerts
        self.clock = clock or time.time
        self.choose_offset = choose_offset or random.randint
        self.state = _load(self.path, {})
        self.lock = asyncio.Lock()

    def snapshot(self, settings):
        now = datetime.fromtimestamp(self.clock(), BEIJING)
        day = now.date()
        if self.state.get('last_run_date') == day.isoformat():
            day += timedelta(days=1)
        signature = [settings.get('checkin_time', '10:00'), settings.get('checkin_jitter_minutes', 5)]
        plan = self.state.get('plan', {})
        if plan.get('date') != day.isoformat() or plan.get('settings') != signature:
            hour, minute = map(int, signature[0].split(':'))
            start = datetime.combine(day, datetime.min.time(), BEIJING)
            center = hour * 60 + minute
            offset = self.choose_offset(max(-signature[1], -center), min(signature[1], 1439 - center))
            chosen = start + timedelta(minutes=center + offset)
            plan = {'date': day.isoformat(), 'settings': signature, 'run_at': chosen.timestamp()}
            self._save({**self.state, 'plan': plan})
        return {'next_checkin_at': self.state['plan']['run_at'] if settings.get('auto_checkin_enabled', True) else None,
                'last_run_at': self.state.get('last_run_at'), 'last_success': self.state.get('last_success'),
                'timezone': 'Asia/Shanghai'}

    def _save(self, state):
        _atomic_json(self.path, state)
        self.state = state

    async def run_due(self):
        async with self.lock:
            settings = (await self.accounts.snapshot())['settings']
            plan = self.snapshot(settings)
            due = plan['next_checkin_at']
            if due is None or self.clock() < due:
                return {'ok': True, 'accounts': [], 'skipped': True, 'next_checkin_at': due}
            day = self.state['plan']['date']
            result = await run_checkin_batch(self.accounts, self.alerts)
            self._save({**self.state, 'last_run_date': day, 'last_run_at': self.clock(), 'last_success': result['ok']})
            return result
