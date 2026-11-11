"""Systemd daily task: one credential owner, one account registry."""
import json
from pathlib import Path
import urllib.request
import urllib.error
import time
from config import STATE,PORT
from checkin_result import checkin_succeeded


def main():
    key=(STATE/'internal-key').read_text().strip()
    request=urllib.request.Request(f'http://127.0.0.1:{PORT}/internal/checkin-due',data=b'{}',
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(12):
        try:
            with opener.open(request,timeout=300) as response:result=json.load(response)
            break
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason,ConnectionRefusedError) or attempt==11:raise
            time.sleep(2)
    # No IDs or credentials in task logs.
    if isinstance(result, dict) and result.get('skipped') is True:
        return 0
    rows = result.get('results', result.get('accounts', [])) if isinstance(result, dict) else result
    failed = sum(not (row.get('ok') is True or checkin_succeeded(row.get('checkin', row))) for row in rows)
    ok = failed == 0 and not (isinstance(result, dict) and result.get('ok') is False)
    print(json.dumps({'accounts': len(rows), 'failed': failed, 'ok': ok}))
    return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
