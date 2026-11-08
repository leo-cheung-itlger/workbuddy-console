"""Hash-only API client credentials; the existing api-key remains a legacy key."""
import contextvars
from datetime import datetime
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid


client_key_context = contextvars.ContextVar('console_client_key', default=None)
SAFE_FIELDS = 'id,name,prefix,created_at,expires_at,revoked_at,last_used_at,legacy'


def token_bytes(token):
    if isinstance(token, str):
        try: token = token.encode('ascii')
        except UnicodeError: return None
    if not isinstance(token, bytes) or not 1 <= len(token) <= 1024:
        return None
    if any(c < 33 or c > 126 or c == 44 for c in token):
        return None
    return token


def expiry_timestamp(value):
    if value in (None, ''): return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None: raise ValueError('Expiry must include a timezone')
            value = parsed.timestamp()
        except (ValueError, OverflowError) as exc:
            raise ValueError('Invalid expiry') from exc
    if type(value) not in (float, int) or not 0 < value <= 253402300799 or value <= time.time():
        raise ValueError('Expiry must be in the future')
    return float(value)


class ClientKeys:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root/'client-keys.sqlite3', check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS client_keys (
                id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,
                revoked_at REAL,last_used_at REAL,legacy INTEGER NOT NULL DEFAULT 0);''')
        legacy = self.root/'api-key'
        if legacy.exists():
            value = token_bytes(legacy.read_bytes().strip())
            if not value: raise ValueError('API key file is invalid')
            # INSERT OR IGNORE preserves a revocation across process restarts.
            with self.db:
                self.db.execute('''INSERT OR IGNORE INTO client_keys
                    (id,name,token_hash,prefix,created_at,legacy) VALUES (?,?,?,?,?,1)''',
                    ('legacy','Existing API key',self._hash(value),'legacy',time.time()))

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token).hexdigest()

    @staticmethod
    def _safe(row):
        data = dict(row)
        data['legacy'] = bool(data['legacy'])
        data['expired'] = data['expires_at'] is not None and data['expires_at'] <= time.time()
        data['active'] = data['revoked_at'] is None and not data['expired']
        return data

    def list(self):
        with self.lock:
            return [self._safe(row) for row in self.db.execute(
                'SELECT '+SAFE_FIELDS+' FROM client_keys ORDER BY created_at DESC').fetchall()]

    def create(self, name, expires_at=None):
        if not isinstance(name,str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError('Key name must contain 1 to 100 characters')
        if any(ord(c)<32 for c in name): raise ValueError('Invalid key name')
        expires_at = expiry_timestamp(expires_at)
        value = 'wbk_'+secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute('''INSERT INTO client_keys
                (id,name,token_hash,prefix,created_at,expires_at) VALUES (?,?,?,?,?,?)''',
                (key_id,name.strip(),self._hash(value.encode()),value[:12],time.time(),expires_at))
            row = self.db.execute('SELECT '+SAFE_FIELDS+' FROM client_keys WHERE id=?',(key_id,)).fetchone()
        return {**self._safe(row),'key':value}

    def revoke(self, key_id):
        with self.lock, self.db:
            result = self.db.execute('UPDATE client_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                                     (time.time(),str(key_id)))
            return bool(result.rowcount)

    def authenticate(self, token):
        token = token_bytes(token)
        if token is None: return None
        with self.lock, self.db:
            row = self.db.execute('SELECT '+SAFE_FIELDS+''' FROM client_keys
                WHERE token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)''',
                (self._hash(token),time.time())).fetchone()
            if row is None: return None
            self.db.execute('UPDATE client_keys SET last_used_at=? WHERE id=?',(time.time(),row['id']))
            # Only the identity is attached to inference/routing context.
            return {'id':row['id'],'name':row['name'],'legacy':bool(row['legacy'])}

    def close(self):
        with self.lock: self.db.close()
