import asyncio
from pathlib import Path
import tempfile
import unittest

from api_auth import APIKeyGuard


class GuardTests(unittest.TestCase):
    def call(self, headers, path='/v1/chat/completions'):
        called = []
        messages = []

        async def app(scope, receive, send):
            called.append(True)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data: OK\n\n', 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'data: [DONE]\n\n'})

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder)/'api-key'
            key.write_text('example-test-key')
            guard = APIKeyGuard(app, key_file=key)
            asyncio.run(guard({'type': 'http', 'path': path, 'headers': headers}, receive, send))
        return called, messages

    def test_missing_and_invalid_key_never_reach_upstream(self):
        for headers in [[], [(b'authorization', b'Bearer wrong')],
                        [(b'authorization', b'Bearer example-test-key'), (b'authorization', b'Bearer wrong')]]:
            called, messages = self.call(headers)
            self.assertFalse(called)
            self.assertEqual(messages[0]['status'], 401)

    def test_bearer_and_anthropic_key_preserve_stream_frames(self):
        for headers in [[(b'authorization', b'Bearer example-test-key')], [(b'x-api-key', b'example-test-key')]]:
            called, messages = self.call(headers)
            self.assertTrue(called)
            self.assertEqual(messages[0]['status'], 200)
            self.assertEqual(messages[1]['body'], b'data: OK\n\n')
            self.assertTrue(messages[1]['more_body'])
            self.assertEqual(messages[2]['body'], b'data: [DONE]\n\n')

    def test_loopback_admin_and_health_remain_available(self):
        for path in ['/health', '/admin']:
            called, messages = self.call([], path)
            self.assertTrue(called)
            self.assertEqual(messages[0]['status'], 200)


if __name__ == '__main__':
    unittest.main()
