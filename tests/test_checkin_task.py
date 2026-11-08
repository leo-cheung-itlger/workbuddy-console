import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import checkin_task


class CheckinTaskTests(unittest.TestCase):
    def test_startup_connection_refused_retries_before_running_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'internal-key').write_text('fake-internal-test-key')
            class Opener:
                calls=0
                def open(self,request,timeout):
                    self.calls+=1
                    if self.calls<3:raise urllib.error.URLError(ConnectionRefusedError())
                    return io.BytesIO(b'{"ok":true,"accounts":[{"checkin":{"status":"checked"}}]}')
            opener=Opener()
            with patch.object(checkin_task,'STATE',root),patch.object(checkin_task.urllib.request,'build_opener',return_value=opener),patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checkin_task.main(),0)
            self.assertEqual(opener.calls,3)
