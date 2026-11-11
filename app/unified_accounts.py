"""Console account policy; upstream AccountStore alone owns credentials.

Only routing flags, cooldowns and projected resource metadata are persisted here.
The upstream forward-once hook is resolved at call time so telemetry can wrap it.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
import copy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import re
from pathlib import Path
import threading
import time
import uuid

from fastapi import HTTPException
from starlette.responses import StreamingResponse
import httpx
from checkin_result import checkin_succeeded


request_session: ContextVar[str | None] = ContextVar('unified_account_session', default=None)
request_namespace: ContextVar[str] = ContextVar('unified_account_namespace', default='')
STRATEGIES = frozenset(('round_robin', 'least_in_flight', 'earliest_expiry', 'fixed'))
ACCOUNT_FIELDS = ('id', 'uid', 'nickname', 'backend', 'active', 'model_cooldowns', 'checkin')
RESOURCE_FIELDS = ('credits', 'credits_total', 'credits_earliest_expiry',
                   'credits_earliest_remaining', 'resource_updated_at', 'resource_error',
                   'credits_expiring_7d', 'expiry_batches', 'expiry_known', 'expiry_updated_at')


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e12 else number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


class UnifiedAccounts:
    def __init__(self, proxy, root):
        self.proxy = proxy
        self.root = Path(root)
        self.path = self.root / 'unified-accounts.json'
        self._lock = threading.RLock()
        self._checkin_lock = asyncio.Lock()
        self._billing_refresh_lock = asyncio.Lock()
        self._sessions = OrderedDict()
        self._inflight = {}
        self._cursors = {}
        self._initialized_manager = None
        self._installed = False
        self._state = dict(version=1, accounts={}, cooldowns={}, resources={},
                           settings=dict(routing_strategy='round_robin', fixed_account_id=None,
                                         auto_switch_on_429=True, auto_checkin_enabled=True,
                                         checkin_time='10:00', checkin_jitter_minutes=5))
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raw = None
        if raw is not None:
            if not isinstance(raw, dict) or raw.get('version') != 1:
                raise ValueError('统一账号设置格式无效，原文件未修改')
            for field in ('accounts', 'cooldowns', 'resources', 'settings'):
                if not isinstance(raw.get(field), dict):
                    raise ValueError('统一账号设置格式无效，原文件未修改')
            self._state = raw
            raw['settings'].setdefault('checkin_time', '10:00')
            raw['settings'].setdefault('checkin_jitter_minutes', 5)
            if raw['settings'].get('routing_strategy') not in STRATEGIES:
                raise ValueError('选号策略无效')
            for item in raw['accounts'].values():
                if not isinstance(item, dict) or any(type(item.get(k)) is not bool for k in ('api_enabled', 'checkin_enabled')):
                    raise ValueError('账号开关格式无效')

    @property
    def manager(self):
        state = getattr(self.proxy, 'proxy_state', None)
        manager = getattr(state, 'account_manager', None)
        if manager is None:
            raise HTTPException(503, detail='账号管理尚未初始化')
        return manager

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with target.open('x', encoding='utf-8') as stream:
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                json.dump(self._state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(target, self.path)
        finally:
            if target.exists():
                target.unlink()

    def _accounts(self):
        manager = self.manager
        snapshot = manager.snapshot()
        with self._lock:
            changed = False
            for account in snapshot.get('accounts', []):
                identifier = account.get('id')
                if identifier and identifier not in self._state['accounts']:
                    self._state['accounts'][identifier] = dict(api_enabled=True, checkin_enabled=True)
                    changed = True
            if self._initialized_manager is not manager:
                if not self.path.exists():
                    self._state['settings']['auto_switch_on_429'] = manager.get_settings().get('auto_switch_on_429', True)
                # The console timer is the sole automatic scheduler. Never start
                # the upstream all-account scheduler, which has no policy flags.
                manager.set_settings(auto_checkin_enabled=False)
                self._initialized_manager = manager
                changed = True
            if changed:
                self._save()
        return snapshot

    def install(self):
        if self._installed:
            return self
        self._installed = True
        self.proxy.forward_chat = self.forward_chat
        app = getattr(self.proxy, 'app', None)
        if app is not None:
            old_start = getattr(self.proxy, 'start_account_checkin', None)
            app.router.on_startup[:] = [fn for fn in app.router.on_startup if fn is not old_start]
            app.router.add_event_handler('startup', self._startup)
        if getattr(getattr(self.proxy, 'proxy_state', None), 'account_manager', None) is not None:
            self._accounts()
        return self

    async def _startup(self):
        await asyncio.to_thread(self._accounts)

    async def snapshot(self):
        snapshot = await asyncio.to_thread(self._accounts)
        now = time.time()
        with self._lock:
            result = []
            for record in snapshot.get('accounts', []):
                identifier = record['id']
                safe = {key: copy.deepcopy(record[key]) for key in ACCOUNT_FIELDS if key in record}
                safe['active'] = identifier == snapshot.get('active_account_id')
                safe.update(self._state['accounts'][identifier])
                safe.update(self._state['resources'].get(identifier, {}))
                if isinstance(safe.get('expiry_batches'), list):
                    safe.update(self._summarize_batches(safe['expiry_batches']))
                deadlines = self._state['cooldowns'].get(identifier, {})
                safe['cooldown_until'] = max([float(x) for x in deadlines.values() if _number(x) is not None] + [0])
                safe['cooling'] = safe['cooldown_until'] > now
                safe['routing_cooldowns'] = {key: value for key, value in deadlines.items() if value > now}
                safe['in_flight'] = self._inflight.get(identifier, 0)
                for key in RESOURCE_FIELDS:
                    safe.setdefault(key, None)
                result.append(safe)
            return dict(accounts=result, active_account_id=snapshot.get('active_account_id'),
                        settings=copy.deepcopy(self._state['settings']), server_time=now)

    async def action(self, data):
        if not isinstance(data, dict):
            raise ValueError('操作格式无效')
        snapshot = await asyncio.to_thread(self._accounts)
        action = data.get('action')
        identifier = data.get('id')
        accounts = {item['id']: item for item in snapshot['accounts']}
        if action != 'settings' and identifier not in accounts:
            raise ValueError('账号不存在')
        if action == 'flags':
            update = {key: data[key] for key in ('api_enabled', 'checkin_enabled') if key in data}
            if not update or any(type(value) is not bool for value in update.values()):
                raise ValueError('账号开关必须为布尔值')
            with self._lock:
                previous = dict(self._state['accounts'][identifier])
                self._state['accounts'][identifier].update(update)
                try:
                    self._save()
                except Exception:
                    self._state['accounts'][identifier] = previous
                    raise
        elif action == 'settings':
            with self._lock:
                previous = dict(self._state['settings'])
                updated = dict(previous)
                for key in ('routing_strategy', 'fixed_account_id', 'auto_switch_on_429', 'auto_checkin_enabled',
                            'checkin_time', 'checkin_jitter_minutes'):
                    if key in data:
                        updated[key] = data[key]
                if updated['routing_strategy'] not in STRATEGIES:
                    raise ValueError('选号策略无效')
                if any(type(updated[key]) is not bool for key in ('auto_switch_on_429', 'auto_checkin_enabled')):
                    raise ValueError('设置开关必须为布尔值')
                if not isinstance(updated['checkin_time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', updated['checkin_time']):
                    raise ValueError('签到时间需为 00:00 至 23:59')
                if type(updated['checkin_jitter_minutes']) is not int or not 0 <= updated['checkin_jitter_minutes'] <= 1440:
                    raise ValueError('随机浮动需为 0 至 1440 的整数分钟')
                if updated.get('fixed_account_id') is not None and updated['fixed_account_id'] not in accounts:
                    raise ValueError('固定账号不存在')
                if updated['routing_strategy'] == 'fixed' and not updated.get('fixed_account_id'):
                    raise ValueError('请选择固定账号')
                self._state['settings'] = updated
                try:
                    self._save()
                except Exception:
                    self._state['settings'] = previous
                    raise
                if updated['routing_strategy'] != previous['routing_strategy'] or updated.get('fixed_account_id') != previous.get('fixed_account_id'):
                    self._sessions.clear()
        elif action == 'activate':
            await asyncio.to_thread(self.manager.activate, identifier)
        elif action == 'delete':
            await asyncio.to_thread(self.manager.delete, identifier)
            with self._lock:
                for field in ('accounts', 'resources', 'cooldowns'):
                    self._state[field].pop(identifier, None)
                self._save()
        elif action == 'checkin':
            return await self.checkin(identifier)
        elif action in ('balance', 'refresh_balance'):
            return await self.refresh_balance(identifier)
        else:
            raise ValueError('操作无效')
        return dict(ok=True)

    def _session_key(self, body, original):
        explicit = request_session.get()
        if explicit:
            value = 'explicit:' + str(explicit)
        else:
            messages = (original or body).get('messages', [])
            first = next((item.get('content') for item in messages if isinstance(item, dict) and item.get('role') == 'user'), None)
            value = 'user:' + json.dumps(first, ensure_ascii=False, sort_keys=True) if first is not None else uuid.uuid4().hex
        return hashlib.sha256((str(request_namespace.get()) + '\0' + value).encode('utf-8')).hexdigest()

    def _choose(self, snapshot, model, session, excluded, region=None, retry=False):
        records = snapshot['accounts']
        active = next((a for a in records if a['id'] == snapshot.get('active_account_id')), None)
        settings = self._state['settings']
        fixed = settings.get('fixed_account_id') if settings['routing_strategy'] == 'fixed' else None
        base = next((a for a in records if a['id'] == fixed), None) if fixed else active
        # Backend and endpoint both form a region boundary. Never send a request
        # to a different service host just because its account is available.
        region = region or ((base.get('backend'), base.get('endpoint')) if base else None)
        if region is None:
            return None, region
        now = time.time()
        eligible = []
        for account in records:
            identifier = account['id']
            if identifier in excluded or not self._state['accounts'][identifier]['api_enabled']:
                continue
            if (account.get('backend'), account.get('endpoint')) != region:
                continue
            if max(self._state['cooldowns'].get(identifier, {}).get(model, 0), self._state['cooldowns'].get(identifier, {}).get('*auth*', 0)) > now:
                continue
            if any(c.get('model') == model and c.get('rate_limited') and (c.get('cooldown_until') or 0) > now for c in account.get('model_cooldowns', [])):
                continue
            if fixed and not retry and identifier != fixed:
                continue
            eligible.append(account)
        sticky = self._sessions.get(session)
        if sticky and sticky[1] > now:
            match = next((a for a in eligible if a['id'] == sticky[0]), None)
            if match:
                return match, region
        if not eligible:
            return None, region
        strategy = settings['routing_strategy']
        cursor = self._cursors.get(region, 0)
        ordered = sorted(eligible, key=lambda a: a['id'])
        rotated = ordered[cursor % len(ordered):] + ordered[:cursor % len(ordered)]
        if strategy == 'least_in_flight':
            selected = min(rotated, key=lambda a: self._inflight.get(a['id'], 0))
        elif strategy == 'earliest_expiry':
            def expiry(account):
                resource = self._state['resources'].get(account['id'], {})
                deadline = _timestamp(resource.get('credits_earliest_expiry'))
                return deadline if deadline and deadline > now and (_number(resource.get('credits_earliest_remaining')) or 0) > 0 else math.inf
            selected = min(rotated, key=expiry)
        else:
            selected = rotated[0]
        self._cursors[region] = cursor + 1
        self._sessions[session] = (selected['id'], now + 86400)
        self._sessions.move_to_end(session)
        while len(self._sessions) > 10000:
            self._sessions.popitem(last=False)
        return selected, region

    def _cooldown(self, identifier, model, response=None, auth=False):
        now = time.time()
        delay = 60.0
        if response is not None:
            retry = response.headers.get('Retry-After')
            number = _number(retry)
            if number is None and retry:
                try:
                    number = parsedate_to_datetime(retry).timestamp() - now
                except (TypeError, ValueError, OverflowError):
                    pass
            if number is not None:
                delay = max(1, min(86400, number))
        with self._lock:
            cooldowns = self._state['cooldowns'].setdefault(identifier, {})
            key = '*auth*' if auth else model
            cooldowns[key] = max(cooldowns.get(key, 0), now + delay)
            # Runtime routing safety still applies if disk is unavailable. An
            # upstream 429/auth error must not be turned into an unrelated 500.
            try:
                self._save()
            except OSError:
                pass

    def _release(self, identifier):
        with self._lock:
            self._inflight[identifier] = max(0, self._inflight.get(identifier, 0) - 1)

    async def forward_chat(self, body, protocol, original=None):
        snapshot = await asyncio.to_thread(self._accounts)
        # AccountStore listing contains the canonical endpoint required for
        # regional isolation; the public manager snapshot intentionally omits it.
        records = await asyncio.to_thread(self.manager.store.list_accounts)
        snapshot['accounts'] = [{**record, **next((s for s in snapshot['accounts'] if s['id'] == record['id']), {})} for record in records]
        session = self._session_key(body, original)
        model = str(body.get('model') or '')
        excluded = set()
        region = None
        last_error = None
        response = None
        while len(excluded) < len(records):
            with self._lock:
                selected, region = self._choose(snapshot, model, session, excluded, region, bool(excluded))
                if selected is None:
                    break
                identifier = selected['id']
                excluded.add(identifier)
                self._inflight[identifier] = self._inflight.get(identifier, 0) + 1
            try:
                try:
                    client = await asyncio.to_thread(self.manager.store.get_client, identifier)
                except Exception:
                    # Loading a saved session performs no upstream request. A
                    # missing/corrupt credential file is unavailable before any
                    # inference, and can safely move to another eligible account.
                    raise HTTPException(401, detail='账号会话不可用，请重新关联') from None
                state = copy.copy(self.proxy.get_request_state())
                state._client = client
                state.account_manager = None
                state.account_runtime = self.manager
                # ProxyState.client is a property; simple adapter states use an
                # ordinary attribute. Keep both types correctly pinned.
                if not isinstance(getattr(type(state), 'client', None), property):
                    state.client = client
                state.unified_account_id = identifier
                response = await self.proxy._forward_chat_once(body, protocol, original, state)
            except BaseException as error:
                self._release(identifier)
                if isinstance(error, HTTPException) and error.status_code == 401:
                    self._cooldown(identifier, model, auth=True)
                    last_error = error
                    if self._state['settings']['auto_switch_on_429']:
                        continue
                raise
            if isinstance(response, StreamingResponse):
                iterator = response.body_iterator
                async def tracked(source=iterator, account_id=identifier):
                    try:
                        async for chunk in source:
                            yield chunk
                    finally:
                        self._release(account_id)
                        close = getattr(source, 'aclose', None)
                        if close:
                            await close()
                response.body_iterator = tracked()
                return response
            self._release(identifier)
            if response.status_code not in (401, 429):
                return response
            self._cooldown(identifier, model, response, auth=response.status_code == 401)
            if not self._state['settings']['auto_switch_on_429']:
                return response
        if response is not None:
            return response
        if last_error is not None:
            raise last_error
        raise HTTPException(503, detail='当前区域没有可用的 API 账号')

    async def checkin(self, identifier):
        snapshot = await asyncio.to_thread(self._accounts)
        if identifier not in {a['id'] for a in snapshot['accounts']}:
            raise ValueError('账号不存在')
        with self._lock:
            if not self._state['accounts'][identifier]['checkin_enabled']:
                raise ValueError('此账号未启用签到')
        return await self._checkin_confirmed(identifier, reason='manual')

    async def _checkin_confirmed(self, identifier, reason):
        service = self.manager.checkins
        result = await service.check_in(identifier, reason=reason)
        if not checkin_succeeded(result) and callable(getattr(service, 'check_status', None)):
            # A rejected/uncertain POST can still have completed, including a
            # concurrent manual check-in. Verify without repeating that POST.
            confirmed = await service.check_status(identifier, reason=reason)
            if checkin_succeeded(confirmed):
                result = confirmed
        if checkin_succeeded(result):
            return {**result, 'status': 'checked', 'checked': True, 'error': None}
        return result

    async def checkin_all(self):
        async with self._checkin_lock:
            snapshot = await asyncio.to_thread(self._accounts)
            if not self._state['settings']['auto_checkin_enabled']:
                return dict(ok=True, accounts=[], skipped=True)
            result = []
            for account in snapshot['accounts']:
                identifier = account['id']
                with self._lock:
                    enabled = self._state['accounts'][identifier]['checkin_enabled']
                if not enabled:
                    continue
                try:
                    checked = await self._checkin_confirmed(identifier, reason='timer')
                    result.append(dict(id=identifier, checkin=checked))
                except Exception:
                    result.append(dict(id=identifier, checkin=dict(status='error', error='签到请求失败')))
            return dict(ok=all(checkin_succeeded(a['checkin']) for a in result), accounts=result)

    async def refresh_balance(self, identifier):
        snapshot = await asyncio.to_thread(self._accounts)
        if identifier not in {a['id'] for a in snapshot['accounts']}:
            raise ValueError('账号不存在')
        client = await asyncio.to_thread(self.manager.store.get_client, identifier)
        try:
            summary = await self._resource_request(client, '/billing/meter/get-user-resource-summary', {})
            resource = self.project_resources(summary)
            if 'credits' not in resource:
                raise ValueError('余额查询失败')
            resource.update(resource_updated_at=time.time(), resource_error=None)
        except Exception:
            with self._lock:
                self._state['resources'].setdefault(identifier, {})['resource_error'] = '余额查询失败'
                self._save()
            raise ValueError('余额查询失败') from None
        try:
            codes = sorted({p['PackageCode'] for p in summary.get('Packages', [])
                            if isinstance(p, dict) and isinstance(p.get('PackageCode'), str)})
            batches = await self._resource_packages(client, 'free', codes)
            if summary.get('IsPaidUser') is True:
                batches += await self._resource_packages(client, 'paid', codes)
            account = next(a for a in snapshot['accounts'] if a['id'] == identifier)
            resource.update(self._project_expiry(batches, account.get('backend')))
            resource.update(expiry_known=True, expiry_updated_at=time.time())
        except Exception:
            # Keep the last complete expiry snapshot and its original timestamp.
            # The successful official total remains useful on its own.
            resource.update(expiry_known=False, resource_error='积分包到期信息查询失败')
        with self._lock:
            self._state['resources'].setdefault(identifier, {}).update(resource)
            self._save()
            return dict(ok=True, id=identifier, **copy.deepcopy(self._state['resources'][identifier]))

    async def _resource_request(self, client, path, body):
        """A fixed official billing POST, with one refresh on a definite 401."""
        await asyncio.to_thread(client.ensure_authenticated, open_browser=False, allow_login=False)
        def billing_headers():
            headers = {key: value for key, value in client.auth_headers().items()
                       if key in ('Authorization', 'X-User-Id', 'X-Enterprise-Id', 'X-Tenant-Id', 'X-Domain')}
            headers.update({'Accept': 'application/json', 'Content-Type': 'application/json',
                            'User-Agent': 'axios/1.16.1', 'X-Request-ID': uuid.uuid4().hex,
                            'Accept-Language': 'en' if getattr(getattr(client, 'profile', None), 'key', '') == 'global' else 'zh'})
            return headers
        async with httpx.AsyncClient(timeout=15) as transport:
            headers = billing_headers()
            response = await transport.post(client.build_url(path), headers=headers, json=body)
            if response.status_code == 401:
                async with self._billing_refresh_lock:
                    latest = billing_headers()
                    if latest.get('Authorization') == headers.get('Authorization'):
                        refreshed = await asyncio.to_thread(client.refresh)
                        latest = billing_headers()
                        if not refreshed or latest.get('Authorization') == headers.get('Authorization'):
                            raise ValueError('余额查询认证失败')
                    if not latest.get('Authorization'):
                        raise ValueError('余额查询认证失败')
                response = await transport.post(client.build_url(path), headers=latest, json=body)
            payload = response.json()
            if response.status_code != 200 or not isinstance(payload, dict) or type(payload.get('code')) is not int or payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                raise ValueError('余额查询失败')
            return payload['data']

    async def _resource_packages(self, client, kind, codes):
        result = []
        for page in range(1, 11):
            body = dict(PackageCodes=codes, Status=[0, 3], PageNumber=page, PageSize=200)
            if kind == 'paid':
                body['NeedRenewInfo'] = True
            payload = await self._resource_request(client, '/billing/meter/get-user-resource-' + kind + '-packages', body)
            accounts = payload.get('Accounts')
            total = _number(payload.get('TotalCount'))
            if not isinstance(accounts, list) or total is None or total < 0 or int(total) != total:
                raise ValueError('积分包响应不完整')
            result.extend(accounts)
            unique = {item.get('ResourceId') for item in result if isinstance(item, dict) and isinstance(item.get('ResourceId'), str)}
            if len(unique) >= total:
                return result
            if not accounts:
                raise ValueError('积分包响应不完整')
        raise ValueError('积分包分页不完整')

    @staticmethod
    def _project_expiry(packages, backend):
        now = time.time()
        seen = set()
        amounts = {}
        one_time = {'007', '009', '038', '029', '030', '036'}
        if backend == 'global':
            one_time.update(('008', '006'))
        for package in packages:
            if not isinstance(package, dict):
                raise ValueError('积分包响应不完整')
            identifier = package.get('ResourceId')
            if not isinstance(identifier, str) or not identifier:
                raise ValueError('积分包标识缺失')
            if identifier in seen:
                continue
            seen.add(identifier)
            remain = _number(package.get('CycleCapacityRemainPrecise'))
            if remain is None:
                raise ValueError('积分包精确余额缺失')
            if remain <= 0:
                continue
            code = str(package.get('PackageCode', '')).removeprefix('TCACA_code_').split('_', 1)[0]
            fields = ('DeductionEndTime', 'ExpiredTime', 'CycleEndTime') if code in one_time else ('CycleEndTime', 'ExpiredTime', 'DeductionEndTime')
            expiry = next((stamp for field in fields if (stamp := _timestamp(package.get(field))) and stamp > 0), None)
            if expiry is None:
                raise ValueError('积分包到期时间缺失')
            if expiry > now:
                amounts[expiry] = amounts.get(expiry, 0) + remain
        batches = [dict(amount=amount, expiry=expiry) for expiry, amount in sorted(amounts.items())]
        return dict(expiry_batches=batches, **UnifiedAccounts._summarize_batches(batches))

    @staticmethod
    def _summarize_batches(batches):
        now = time.time()
        upcoming = sorted((b for b in batches if b['expiry'] > now), key=lambda b: b['expiry'])
        beijing = timezone(timedelta(hours=8))
        first_date = datetime.fromtimestamp(upcoming[0]['expiry'], beijing).date() if upcoming else None
        return dict(credits_expiring_7d=sum(b['amount'] for b in upcoming if b['expiry'] <= now + 7 * 86400),
                    credits_earliest_expiry=upcoming[0]['expiry'] if upcoming else None,
                    credits_earliest_remaining=sum(b['amount'] for b in upcoming
                        if datetime.fromtimestamp(b['expiry'], beijing).date() == first_date))

    @staticmethod
    def project_resources(data):
        """Project explicit observed fields; absent totals/expiry stay unknown.

        This accepts official summary fields and sanitized migration metadata,
        never retains a raw response or interprets quota as remaining credits.
        """
        result = {}
        for target, keys in {
            'credits': ('credits', 'TotalCredits'),
            'credits_total': ('credits_total',),
            'credits_earliest_remaining': ('credits_earliest_remaining',),
        }.items():
            for key in keys:
                if key in data and _number(data[key]) is not None:
                    result[target] = _number(data[key])
                    break
        packages = data.get('Packages')
        if 'credits_total' not in result and isinstance(packages, list) and packages:
            capacities = [_number(item.get('CycleTotalCapacity')) if isinstance(item, dict) else None for item in packages]
            if all(value is not None and value >= 0 for value in capacities):
                result['credits_total'] = sum(capacities)
        expiry = _timestamp(data.get('credits_earliest_expiry'))
        if expiry is not None:
            result['credits_earliest_expiry'] = expiry
        return result

    async def seed_resources(self, identifier, data):
        """One-time migration of already sanitized balances; no credentials."""
        snapshot = await asyncio.to_thread(self._accounts)
        if identifier not in {a['id'] for a in snapshot['accounts']}:
            raise ValueError('账号不存在')
        with self._lock:
            if identifier not in self._state['resources']:
                safe = self.project_resources(data)
                safe['resource_updated_at'] = time.time()
                self._state['resources'][identifier] = safe
                self._save()
