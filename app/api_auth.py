"""Credential-file backed authentication for loopback model API routes."""
import hmac
from pathlib import Path

from client_keys import client_key_context, token_bytes


class APIKeyGuard:
    def __init__(self, app, key_file=None, keys=None):
        self.app = app
        self.keys = keys
        self.key = Path(key_file).read_bytes().strip() if key_file is not None else None
        if keys is None and not self.key:
            raise ValueError('API key file is empty')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/v1/'):
            return await self.app(scope, receive, send)
        authorization = [v for k, v in scope.get('headers', []) if k.lower() == b'authorization']
        alternative = [v for k, v in scope.get('headers', []) if k.lower() == b'x-api-key']
        candidate = None
        if len(authorization) == 1 and not alternative and authorization[0][:7].lower() == b'bearer ':
            candidate = authorization[0][7:]
        elif not authorization and len(alternative) == 1:
            candidate = alternative[0]
        candidate = token_bytes(candidate)
        owner = self.keys.authenticate(candidate) if self.keys is not None and candidate else None
        if self.keys is None and candidate is not None and hmac.compare_digest(candidate, self.key):
            owner = {'id':'legacy','name':'Existing API key','legacy':True}
        if owner is None:
            body = b'{"error":{"message":"Invalid API key","type":"authentication_error"}}'
            await send({'type': 'http.response.start', 'status': 401,
                'headers': [(b'content-type', b'application/json'), (b'www-authenticate', b'Bearer')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        scope['client_key'] = owner
        scope.setdefault('state', {})['client_key'] = owner
        token = client_key_context.set(owner)
        try:
            await self.app(scope, receive, send)
        finally:
            client_key_context.reset(token)
