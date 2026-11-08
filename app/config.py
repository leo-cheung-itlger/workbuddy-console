"""Deployment-specific values are environment settings, never public source secrets."""
import os
from pathlib import Path

STATE=Path(os.environ.get('WB_STATE_DIR','/var/lib/workbuddy-python'))
ORIGIN=os.environ.get('WB_PUBLIC_ORIGIN','https://console.example.com').rstrip('/')
HOST=os.environ.get('WB_HOST','127.0.0.1')
PORT=int(os.environ.get('WB_PORT','8787'))
API_BASE=ORIGIN+'/v1'
STATUS_FILE=Path(os.environ.get('WB_STATUS_FILE','/run/workbuddy-console-status/status.json'))
