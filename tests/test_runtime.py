import json
from pathlib import Path
import tempfile
import types
import unittest

from runtime import InternalTasks,SessionBinding,alert_result
from unified_accounts import request_session,request_namespace


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_success_is_failure_but_client_cancel_is_ignored(self):
        self.assertEqual(alert_result({'status':200,'complete':0,'connection_status':'connected'})['status'],502)
        self.assertEqual(alert_result({'status':200,'complete':0,'connection_status':'timeout'})['status'],0)
        self.assertIsNone(alert_result({'status':200,'complete':0,'connection_status':'cancelled'}))
        self.assertEqual(alert_result({'status':200,'complete':1,'connection_status':'connected'})['status'],200)

    async def test_session_binding_partitions_keys_and_resets_context(self):
        observed=[]
        async def app(scope,receive,send):observed.append((request_namespace.get(),request_session.get()))
        wrapped=SessionBinding(app)
        scope={'type':'http','headers':[(b'x-session-id',b'conversation-a')],'client_key':{'id':'key-one'}}
        await wrapped(scope,None,None)
        self.assertEqual(observed,[('key-one','key-one:conversation-a')])
        self.assertIsNone(request_session.get())
        self.assertEqual(request_namespace.get(),'')

    async def test_internal_task_requires_loopback_distinct_key_and_no_proxy_headers(self):
        calls=[]
        async def checkin():calls.append(True);return {'ok':True,'accounts':[{'id':'a','checkin':{'status':'checked'}}]}
        async def observe(rows):calls.append(rows)
        async def due():calls.append('due');return {'ok':True,'skipped':True}
        with tempfile.TemporaryDirectory() as tmp:
            wrapped=InternalTasks(None,Path(tmp),types.SimpleNamespace(checkin_all=checkin),
                                  types.SimpleNamespace(observe_checkin=observe),types.SimpleNamespace(run_due=due))
            async def invoke(peer,headers,path='/internal/checkin-all'):
                output=[]
                async def send(message):output.append(message)
                await wrapped({'type':'http','path':path,'method':'POST','client':(peer,1),'headers':headers},None,send)
                return output[0]['status']
            auth=[(b'authorization',b'Bearer '+wrapped.key)]
            self.assertEqual(await invoke('127.0.0.1',[]),403)
            self.assertEqual(await invoke('8.8.8.8',auth),403)
            self.assertEqual(await invoke('127.0.0.1',auth+[(b'cf-connecting-ip',b'8.8.8.8')]),403)
            self.assertEqual(calls,[])
            self.assertEqual(await invoke('127.0.0.1',auth),200)
            self.assertEqual(calls[-1],[{'account_id':'a','ok':True}])
            self.assertEqual(await invoke('127.0.0.1',[],'/internal/checkin-due'),403)
            self.assertEqual(await invoke('127.0.0.1',auth,'/internal/checkin-due'),200)
            self.assertEqual(calls[-1],'due')


if __name__=='__main__':unittest.main()
