"""Authenticated, same-origin console with a small allowlisted management API."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse, RedirectResponse
from starlette.routing import Route
from console_telemetry import Ledger

from config import ORIGIN
COOKIE='__Secure-wb_console'
ASSETS=Path(__file__).parent/'console_assets'


def password_hash(password,salt):
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()


def initialize_auth(root,username,password):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/'console-auth.json'
    if path.exists():raise ValueError('Console administrator already initialized')
    salt=secrets.token_hex(16)
    path.write_text(json.dumps({'username':username,'salt':salt,'hash':password_hash(password,salt)}),encoding='utf-8')
    path.chmod(0o600)


class Auth:
    def __init__(self,root):
        self.root=Path(root)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.root/'console-sessions.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA max_page_count=1024;
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,csrf TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS failures(ip TEXT,ts REAL);
            CREATE INDEX IF NOT EXISTS failure_time ON failures(ts);''')

    def credentials(self):return json.loads((self.root/'console-auth.json').read_text(encoding='utf-8'))

    def session(self,request):
        value=request.cookies.get(COOKIE,'')
        if not value or len(value)>128:return None
        digest=hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            row=self.db.execute('SELECT * FROM sessions WHERE token=? AND expires>?',(digest,time.time())).fetchone()
            return dict(row) if row else None

    def login(self,username,password,ip):
        with self.lock:
            now=time.time();ip=hashlib.sha256(ip.encode()).hexdigest()
            self.db.execute('DELETE FROM failures WHERE ts<?',(now-900,))
            self.db.execute('DELETE FROM sessions WHERE expires<?',(now,))
            count=self.db.execute('SELECT count(*) FROM failures WHERE ip=?',(ip,)).fetchone()[0]
            total=self.db.execute('SELECT count(*) FROM failures').fetchone()[0]
            if count>=5 or total>=100:return 429,None
            auth=self.credentials()
            valid=hmac.compare_digest(password_hash(password,auth['salt']),auth['hash'])
            if not valid or not hmac.compare_digest(username,auth['username']):
                self.db.execute('INSERT INTO failures VALUES (?,?)',(ip,now));self.db.commit()
                return 401,None
            self.db.execute('DELETE FROM failures WHERE ip=?',(ip,))
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,now+7*86400))
            # Limit sessions to the most recent 20, including concurrent phone/desktop sessions.
            self.db.execute('DELETE FROM sessions WHERE token NOT IN (SELECT token FROM sessions ORDER BY expires DESC LIMIT 20)')
            self.db.commit()
            return 200,token

    def logout(self,session):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE token=?',(session['token'],));self.db.commit()

    def change_password(self,current,new):
        with self.lock:
            auth=self.credentials()
            if not hmac.compare_digest(password_hash(current,auth['salt']),auth['hash']):return False
            auth['salt']=secrets.token_hex(16);auth['hash']=password_hash(new,auth['salt'])
            target=self.root/'console-auth.json';tmp=self.root/'console-auth.tmp'
            with open(tmp,'w',encoding='utf-8') as f:
                os.chmod(tmp,0o600);json.dump(auth,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,target)
            self.db.execute('DELETE FROM sessions');self.db.commit()
            return True


async def payload(request):
    if request.headers.get('content-type','').split(';')[0]!='application/json':raise ValueError('需要 JSON 请求')
    data=b''
    async for chunk in request.stream():
        data+=chunk
        if len(data)>512*1024:raise ValueError('请求过大')
    value=json.loads(data or b'{}')
    if not isinstance(value,dict):raise ValueError('请求格式无效')
    return value


def create_console(root,backend,ledger=None):
    root=Path(root);auth=Auth(root);ledger=ledger or Ledger(root/'console.sqlite3')

    async def endpoint(request):
        path=request.url.path
        if path in ('/console','/console/') and request.method=='GET':
            return FileResponse(ASSETS/'index.html',media_type='text/html')
        if path.startswith('/console/assets/') and request.method=='GET':
            filename=path.removeprefix('/console/assets/')
            if filename in {'app.js','app.css'}:
                return FileResponse(ASSETS/filename,media_type='text/javascript' if filename.endswith('.js') else 'text/css')
            return JSONResponse({'error':'未找到页面'},404)
        if not path.startswith('/console/api/'):return JSONResponse({'error':'未找到页面'},404)
        # All mutation endpoints require the exact public Origin, including login.
        if request.method!='GET' and request.headers.getlist('origin')!=[ORIGIN]:
            return JSONResponse({'error':'跨站请求已拒绝'},403)
        if path=='/console/api/login' and request.method=='POST':
            data=await payload(request)
            username,password=data.get('username'),data.get('password')
            if not isinstance(username,str) or not isinstance(password,str) or len(username)>100 or len(password)>256:
                return JSONResponse({'error':'账号或密码无效'},400)
            ip=request.headers.get('cf-connecting-ip') or (request.client.host if request.client else 'unknown')
            status,token=await asyncio.to_thread(auth.login,username,password,ip)
            response=JSONResponse({'ok':True} if status==200 else {'error':'尝试过多，请 15 分钟后重试' if status==429 else '账号或密码错误'},status)
            if token:response.set_cookie(COOKIE,token,max_age=7*86400,path='/console',secure=True,httponly=True,samesite='strict')
            return response
        session=auth.session(request)
        if not session:return JSONResponse({'error':'请先登录'},401)
        if request.method!='GET':
            csrf=request.headers.getlist('x-console-csrf')
            if len(csrf)!=1 or not hmac.compare_digest(csrf[0],session['csrf']):
                return JSONResponse({'error':'会话校验失败，请重新登录'},403)
        if path=='/console/api/me' and request.method=='GET':
            return JSONResponse({'username':auth.credentials()['username'],'csrf':session['csrf'],'expires':session['expires']})
        if path=='/console/api/logout' and request.method=='POST':
            auth.logout(session)
            response=JSONResponse({'ok':True});response.delete_cookie(COOKIE,path='/console',secure=True,httponly=True,samesite='strict')
            return response
        if path=='/console/api/password' and request.method=='POST':
            data=await payload(request)
            if not isinstance(data.get('current'),str) or not isinstance(data.get('password'),str) or not 12<=len(data['password'])<=128 or len(data['current'])>256:
                return JSONResponse({'error':'新密码需为 12–128 个字符'},400)
            valid=await asyncio.to_thread(auth.change_password,data['current'],data['password'])
            return JSONResponse({'ok':True} if valid else {'error':'当前密码错误'},200 if valid else 401)
        if path=='/console/api/requests' and request.method=='GET':
            try:page=int(request.query_params.get('page','1'))
            except ValueError:return JSONResponse({'error':'页码无效'},400)
            return JSONResponse(ledger.requests(page,request.query_params.get('model','')[:150]))
        if path=='/console/api/snapshot' and request.method=='GET':
            return JSONResponse({**await backend.snapshot(),'usage':ledger.summary()})
        if path=='/console/api/models' and request.method=='GET':return JSONResponse(await backend.models())
        if path=='/console/api/catalog-refresh' and request.method=='POST':return JSONResponse(await backend.models(force=True))
        if path=='/console/api/keys' and request.method=='GET':return JSONResponse({'keys':backend.keys.list()})
        if path=='/console/api/keys' and request.method=='POST':
            data=await payload(request)
            return JSONResponse(backend.keys.create(data.get('name',''),data.get('expires_at')))
        if path=='/console/api/key-revoke' and request.method=='POST':
            data=await payload(request);backend.keys.revoke(data.get('id',''));return JSONResponse({'ok':True})
        if path=='/console/api/legacy-key' and request.method=='POST':
            # Explicit CSRF-protected administrator action; never embed the legacy key in a bundle.
            if backend.keys.authenticate((root/'api-key').read_text().strip()) is None:
                return JSONResponse({'error':'原有密钥已停用，请创建新密钥'},409)
            return JSONResponse({'key':(root/'api-key').read_text().strip()})
        if path=='/console/api/mail' and request.method=='GET':return JSONResponse(backend.alerts.settings())
        if path=='/console/api/mail' and request.method=='POST':return JSONResponse(backend.alerts.configure(await payload(request)))
        if path=='/console/api/mail-test' and request.method=='POST':return JSONResponse(await backend.alerts.send_test())
        if path=='/console/api/benchmarks' and request.method=='GET':return JSONResponse(backend.benchmarks.snapshot())
        if path=='/console/api/benchmarks' and request.method=='POST':return JSONResponse(backend.benchmarks.import_records(await payload(request)))
        if path=='/console/api/benchmark-update' and request.method=='POST':return JSONResponse(await backend.benchmarks.update_livebench())
        if path=='/console/api/action' and request.method=='POST':
            data=await payload(request)
            return JSONResponse(await backend.action(data))
        return JSONResponse({'error':'未找到接口'},404)

    async def guarded(request):
        try:response=await endpoint(request)
        except (ValueError,TypeError,json.JSONDecodeError):response=JSONResponse({'error':'请求参数无效'},400)
        except Exception:response=JSONResponse({'error':'服务暂时无法完成请求，请稍后重试'},502)
        response.headers.update({'Cache-Control':'no-store','Pragma':'no-cache','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response
    app=Starlette(routes=[Route('/{path:path}',guarded,methods=['GET','POST'])])
    def close():
        auth.db.close();ledger.db.close()
    app.state.close=close
    return app


class ConsoleDispatch:
    def __init__(self,app,console):self.app,self.console=app,console
    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        target=self.console if scope['type']=='http' and (path=='/console' or path.startswith('/console/')) else self.app
        await target(scope,receive,send)
