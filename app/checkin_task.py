"""Systemd daily task: one credential owner, one account registry."""
import json
from pathlib import Path
import urllib.request
import urllib.error
import time
from config import STATE,PORT


def main():
    key=(STATE/'internal-key').read_text().strip()
    request=urllib.request.Request(f'http://127.0.0.1:{PORT}/internal/checkin-all',data=b'{}',
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
    rows=result.get('results',result.get('accounts',[])) if isinstance(result,dict) else result
    failed=sum(not row.get('ok',row.get('checked',row.get('checkin',{}).get('status')=='checked')) for row in rows)
    print(json.dumps({'accounts':len(rows),'failed':failed,'ok':failed==0}))
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
