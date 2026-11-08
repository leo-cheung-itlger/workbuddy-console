import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
import unittest

from api_auth import APIKeyGuard


class ClientKeyTests(unittest.TestCase):
    def test_legacy_and_new_keys_survive_restart_without_plaintext_storage(self):
        from client_keys import ClientKeys
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            root = Path(folder)
            (root/'api-key').write_text('legacy-example-test-key')
            keys = ClientKeys(root)
            resources.callback(keys.close)
            self.assertEqual(keys.authenticate('legacy-example-test-key')['id'], 'legacy')
            new = keys.create('editor', expires_at=time.time()+3600)
            self.assertEqual(keys.authenticate(new['key'])['name'], 'editor')
            self.assertNotIn(new['key'], json.dumps(keys.list()))
            keys.close()
            self.assertNotIn(new['key'].encode(), (root/'client-keys.sqlite3').read_bytes())
            keys = ClientKeys(root)
            resources.callback(keys.close)
            self.assertEqual(keys.authenticate(new['key'])['id'], new['id'])
            self.assertTrue(keys.revoke(new['id']))
            self.assertIsNone(keys.authenticate(new['key']))
            self.assertTrue(keys.revoke('legacy'))
            self.assertIsNone(keys.authenticate('legacy-example-test-key'))
            keys.close()

    def test_expiry_is_checked_on_every_authentication(self):
        from client_keys import ClientKeys
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            keys = ClientKeys(folder)
            resources.callback(keys.close)
            new = keys.create('short lived', expires_at=time.time()+0.1)
            self.assertIsNotNone(keys.authenticate(new['key']))
            time.sleep(0.12)
            self.assertIsNone(keys.authenticate(new['key']))
            for value in ['', 'x'*101, None]:
                with self.assertRaises(ValueError): keys.create(value)
            with self.assertRaises(ValueError): keys.create('overflow', expires_at=10**1000)
            keys.close()

    def test_guard_rejects_ambiguous_headers_and_passes_safe_owner(self):
        from client_keys import ClientKeys, client_key_context
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            keys = ClientKeys(folder)
            resources.callback(keys.close)
            new = keys.create('client one')
            observed = []
            async def app(scope, receive, send):
                observed.append((scope['client_key'], client_key_context.get()))
                await send({'type':'http.response.start', 'status':200})
            guard = APIKeyGuard(app, keys=keys)
            async def call(headers):
                sent = []
                async def receive(): return {'type':'http.request', 'body':b''}
                async def send(message): sent.append(message)
                await guard({'type':'http','path':'/v1/models','headers':headers}, receive, send)
                return sent[0]['status']
            bearer = (b'authorization', ('Bearer '+new['key']).encode())
            alternative = (b'x-api-key', new['key'].encode())
            for headers in [[bearer, bearer], [bearer, alternative], [alternative, alternative],
                            [(b'authorization', b'Basic x'), alternative],
                            [(b'authorization', ('Bearer '+new['key']+' ').encode())]]:
                self.assertEqual(asyncio.run(call(headers)), 401)
            self.assertEqual(asyncio.run(call([bearer])), 200)
            self.assertEqual(observed[0][0]['id'], new['id'])
            self.assertEqual(observed[0][0], observed[0][1])
            self.assertNotIn(new['key'], json.dumps(observed))
            keys.revoke(new['id'])
            self.assertEqual(asyncio.run(call([alternative])), 401)
            self.assertIsNone(client_key_context.get())
            keys.close()
