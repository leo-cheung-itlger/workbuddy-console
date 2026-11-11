"""Compose the unmodified pinned upstream and the console's optional services."""
import asyncio
import logging.handlers
from pathlib import Path
import sys
from api_auth import APIKeyGuard
from client_keys import ClientKeys
from console_app import ConsoleDispatch,create_console
from console_backend import Backend
from console_telemetry import Ledger,RequestLedger,install_observation
from model_catalog import ModelCatalog,install_models_endpoint
from unified_accounts import UnifiedAccounts
from mail_alerts import AlertManager
from benchmarks import Benchmarks
from runtime import SessionBinding,InternalTasks,install_workers,alert_result
from config import STATE,HOST,PORT
import codebuddy_proxy.__main__ as proxy


class BoundedLogHandler(logging.handlers.RotatingFileHandler):
    def __init__(self,filename,*args,**kwargs):
        super().__init__(filename,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')


def main():
    root=STATE;root.mkdir(parents=True,exist_ok=True)
    ledger=Ledger(root/'console.sqlite3');keys=ClientKeys(root)
    alerts=AlertManager(root);accounts=UnifiedAccounts(proxy,root);catalog=ModelCatalog(proxy,root)
    benchmarks=Benchmarks(root);backend=Backend(root,catalog,accounts,ledger,keys,alerts,benchmarks)
    def observed(row):
        event=alert_result(row)
        if event is not None:asyncio.get_running_loop().create_task(alerts.observe(event))
    def default_effort(model):
        for entry in catalog.entries.values():
            for row in entry.get('models',[]):
                if row['id']==model:return row.get('default_reasoning_level')
        return None
    install_models_endpoint(proxy,catalog)
    accounts.install()
    install_observation(proxy,ledger,on_result=observed,effort_default=default_effort)
    proxy.app.add_middleware(RequestLedger,ledger=ledger)
    proxy.app.add_middleware(SessionBinding)
    proxy.app.add_middleware(APIKeyGuard,key_file=root/'api-key',keys=keys)
    console=create_console(root,backend,ledger)
    proxy.app.add_middleware(ConsoleDispatch,console=console)
    proxy.app.add_middleware(InternalTasks,root=root,accounts=accounts,alerts=alerts,schedule=backend.checkin_schedule)
    install_workers(proxy,accounts,backend,alerts)
    proxy.ObservedTimedRotatingFileHandler=BoundedLogHandler
    sys.argv=[sys.argv[0],'--host',HOST,'--port',str(PORT),'--accounts-dir',str(root/'accounts'),
              '--log-file',str(root/'logs/service.jsonl')]
    return proxy.main()


if __name__=='__main__':raise SystemExit(main())
