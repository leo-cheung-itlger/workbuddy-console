"""Passive, persisted SMTP alerts. Call only from authenticated administrator APIs.

Observations accept metadata, never response bodies or credentials. SMTP delivery
runs in a worker thread; flush() drains it for shutdown. A process interrupted
after SMTP starts cannot know whether the remote server accepted the email, so
that outbox item is marked interrupted instead of risking duplicate delivery.
"""
import asyncio
from email.message import EmailMessage
import ipaddress
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile
import time
import uuid


DEFAULTS = {'enabled': False, 'host': '', 'port': 587, 'security': 'starttls',
            'username': '', 'password': '', 'from_email': '', 'to_emails': []}
RESTRICTION_CODES = {'ACCOUNT_RESTRICTED', 'ACCOUNT_BANNED', 'ACCOUNT_DISABLED', 'USER_FORBIDDEN'}
EXPIRY_CODES = {'TOKEN_EXPIRED', 'CREDENTIAL_EXPIRED', 'AUTH_EXPIRED'}
SAFE_ID = re.compile(r'^[A-Za-z0-9_.:/@+-]{1,160}$')
ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=True, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path, default):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else default
    except FileNotFoundError:
        return default
    # Corruption must not silently reset incident de-duplication or enable mail.


def _text(value, field, maximum=255):
    if not isinstance(value, str) or len(value) > maximum or '\r' in value or '\n' in value:
        raise ValueError('Invalid SMTP ' + field)
    return value


def _address(value):
    value = _text(value, 'email address', 254)
    if not ADDRESS.fullmatch(value) or '..' in value or value.count('@') != 1:
        raise ValueError('Invalid SMTP email address')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or '.' not in domain or any(not label or len(label) > 63 or
            label.startswith('-') or label.endswith('-') for label in domain.split('.')):
        raise ValueError('Invalid SMTP email address')
    return value


def _identifier(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else 'unknown'


def smtp_send(config, message):
    """TLS is mandatory and uses the system CA store and hostname verification."""
    context = ssl.create_default_context()
    if config['security'] == 'ssl':
        connection = smtplib.SMTP_SSL(config['host'], config['port'], timeout=10, context=context)
    else:
        connection = smtplib.SMTP(config['host'], config['port'], timeout=10)
    with connection as client:
        if config['security'] == 'starttls':
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if config['username']:
            client.login(config['username'], config['password'])
        refused = client.send_message(message, from_addr=config['from_email'], to_addrs=config['to_emails'])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


class AlertManager:
    threshold = 3
    window = 300
    cooldown = 3600

    def __init__(self, root, sender=None, clock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_path = self.root / 'mail-alerts-settings.json'
        self.state_path = self.root / 'mail-alerts-state.json'
        stored = _load(self.config_path, {})
        self._config = {key: stored.get(key, value) for key, value in DEFAULTS.items()}
        self._state = _load(self.state_path, {'incidents': {}, 'outbox': [], 'delivery': {'status': 'idle'}})
        self._sender = sender or smtp_send
        self._clock = clock or time.time
        self._worker = None
        self._send_lock = asyncio.Lock()
        changed = False
        for item in self._state['outbox']:
            if item['status'] == 'sending':
                item['status'] = 'interrupted'
                self._state['delivery'] = {'status': 'failed', 'error': 'Delivery interrupted; acceptance unknown'}
                changed = True
        if changed:
            self._save()

    def settings(self):
        safe = {key: value for key, value in self._config.items() if key != 'password'}
        safe['to_emails'] = list(safe['to_emails'])
        safe['password_set'] = bool(self._config['password'])
        safe['delivery'] = dict(self._state['delivery'])
        safe['active_incidents'] = sum(bool(i['active']) for i in self._state['incidents'].values())
        return safe

    def configure(self, data):
        if not isinstance(data, dict):
            raise ValueError('Invalid SMTP settings')
        config = dict(self._config)
        for key in DEFAULTS:
            if key in data and key != 'password':
                config[key] = data[key]
        if type(config['enabled']) is not bool:
            raise ValueError('Invalid SMTP enabled flag')
        if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
            raise ValueError('Invalid SMTP port')
        if config['security'] not in ('ssl', 'starttls'):
            raise ValueError('SMTP security must be ssl or starttls')
        host = _text(config['host'], 'host').strip()
        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or any(
                        not part or part.startswith('-') or part.endswith('-') or len(part) > 63
                        for part in host.split('.')):
                    raise ValueError('Invalid SMTP host') from None
        config['host'] = host
        config['username'] = _text(config['username'], 'username')
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 4096:
            raise ValueError('Invalid SMTP password')
        if 'clear_password' in data and type(data['clear_password']) is not bool:
            raise ValueError('Invalid SMTP clear password flag')
        if data.get('clear_password'):
            config['password'] = ''
        elif password:
            config['password'] = password
        if config['from_email']:
            config['from_email'] = _address(config['from_email'])
        if not isinstance(config['to_emails'], list) or len(config['to_emails']) > 20:
            raise ValueError('Invalid SMTP recipients')
        config['to_emails'] = list(dict.fromkeys(_address(item) for item in config['to_emails']))
        if config['enabled'] and not (host and config['from_email'] and config['to_emails']):
            raise ValueError('Enabled SMTP requires host, sender and recipients')
        _atomic_json(self.config_path, config)
        self._config = config
        return self.settings()

    def _save(self):
        self._state['outbox'] = self._state['outbox'][-200:]
        _atomic_json(self.state_path, self._state)

    def _queue(self, category, scope, recovered=False, code=''):
        if not self._config['enabled']:
            return
        kind = 'Recovered' if recovered else 'Warning'
        item = {'id': uuid.uuid4().hex, 'status': 'queued', 'created_at': self._clock(),
                'subject': 'WorkBuddy ' + kind + ': ' + category,
                'body': '\n'.join([kind, 'category: ' + category, 'scope: ' + scope,
                                   'business_code: ' + code] if code else
                                  [kind, 'category: ' + category, 'scope: ' + scope])}
        self._state['outbox'].append(item)
        self._save()
        self._start_worker()

    def _start_worker(self):
        if self._config['enabled'] and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._drain())

    async def _deliver(self, item):
        async with self._send_lock:
            if item['status'] != 'queued':
                return {'ok': item['status'] == 'sent', 'status': item['status']}
            config = dict(self._config)
            if not config['enabled']:
                return {'ok': False, 'status': 'disabled'}
            message = EmailMessage()
            message['Subject'] = item['subject']
            message['From'] = config['from_email']
            message['To'] = ', '.join(config['to_emails'])
            message['Message-ID'] = '<' + item['id'] + '@workbuddy-console.local>'
            message.set_content(item['body'])
            item['status'] = 'sending'
            self._save()
            try:
                await asyncio.to_thread(self._sender, config, message)
            except Exception:
                # SMTP exception text may include a password, username or response.
                result = {'ok': False, 'status': 'failed', 'error': 'SMTP delivery failed; check SMTP settings'}
            else:
                result = {'ok': True, 'status': 'sent'}
            item['status'] = result['status']
            self._state['delivery'] = {**result, 'at': self._clock()}
            self._save()
            return result

    async def _drain(self):
        while self._config['enabled']:
            item = next((entry for entry in self._state['outbox'] if entry['status'] == 'queued'), None)
            if item is None:
                return
            await self._deliver(item)

    async def flush(self):
        self._start_worker()
        if self._worker:
            await self._worker

    async def send_test(self):
        """An explicit admin action; no timer or observation invokes this method."""
        if not self._config['enabled']:
            return {'ok': False, 'status': 'disabled'}
        item = {'id': uuid.uuid4().hex, 'status': 'queued', 'created_at': self._clock(),
                'subject': 'WorkBuddy SMTP test', 'body': 'SMTP test requested by an administrator.'}
        self._state['outbox'].append(item)
        return await self._deliver(item)

    def _record(self, kind, account, model, category, code='', immediate=False):
        now = self._clock()
        scope = json.dumps([kind, account, model], separators=(',', ':'))
        key = json.dumps([kind, account, model, category], separators=(',', ':'))
        incident = self._state['incidents'].setdefault(key, {
            'scope': scope, 'category': category, 'active': False, 'events': [], 'last_notified': None})
        incident['events'] = [stamp for stamp in incident['events'] if now - stamp <= self.window][-19:] + [now]
        if len(incident['events']) >= self.threshold or immediate:
            last = incident['last_notified']
            incident['active'] = True
            if self._config['enabled'] and (last is None or now - last >= self.cooldown):
                incident['last_notified'] = now
                self._queue(category, scope, code=code)
        self._save()

    def _recover(self, kind, account, model):
        scope = json.dumps([kind, account, model], separators=(',', ':'))
        for incident in self._state['incidents'].values():
            if incident['scope'] != scope:
                continue
            notified = incident['active'] and incident['last_notified'] is not None
            incident.update(active=False, events=[], last_notified=None)
            if notified:
                self._queue(incident['category'], scope, recovered=True)
        self._save()

    async def observe(self, result):
        """Metadata: account_id/account, model, status, source, business_code.

        source='upstream' is required to classify 401/403 as account auth errors.
        Incoming API-key failures are never treated as account restrictions.
        """
        if not isinstance(result, dict):
            return
        account = _identifier(result.get('account_id', result.get('account')))
        model = _identifier(result.get('model'))
        status = result.get('status', 0)
        if type(status) is not int:
            return
        upstream = result.get('source') == 'upstream'
        code = result.get('business_code', '')
        code = code.upper() if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else ''
        if status in (401, 403) and not upstream:
            return
        if upstream and code in RESTRICTION_CODES:
            category = 'explicit_restriction'
        elif upstream and code in EXPIRY_CODES:
            category = 'credential_expiry'
        elif status == 429:
            category = 'rate_limit'
        elif status in (401, 403):
            category = 'upstream_auth'
        elif status == 0:
            category = 'network_error'
        elif 200 <= status < 400:
            self._recover('api', account, model)
            return
        else:
            category = 'api_failure'
        self._record('api', account, model, category, code)

    async def observe_checkin(self, results):
        """Consume final per-account results after registry-owned retry handling."""
        if isinstance(results, dict):
            results = results.get('results', [results])
        if not isinstance(results, (list, tuple)):
            return
        for result in results:
            if not isinstance(result, dict):
                continue
            account = _identifier(result.get('account_id', result.get('account', result.get('id'))))
            if result.get('ok') is True or result.get('status') in ('success', 'succeeded', 'already_done'):
                self._recover('checkin', account, '')
            elif result.get('ok') is False or result.get('status') == 'failed':
                self._record('checkin', account, '', 'checkin_failure', immediate=True)

    async def check_services(self, results=None):
        """Accept health booleans supplied by root; never query privileged state."""
        if not isinstance(results, dict):
            return
        for service, healthy in results.items():
            if healthy is True:
                self._recover('service', _identifier(service), '')
            elif healthy is False:
                self._record('service', _identifier(service), '', 'service_unavailable', immediate=True)

    async def run(self, check_services=None, interval=60):
        """Run optional ordinary health callback only; no model inference probes."""
        while True:
            if check_services:
                try:
                    await self.check_services(await check_services())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            self._start_worker()
            await asyncio.sleep(max(5, interval))
