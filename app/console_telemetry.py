"""Bounded metadata ledger and byte-preserving upstream usage observation."""
import asyncio
import contextvars
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import types
import uuid

import httpx

from client_keys import client_key_context

request_context = contextvars.ContextVar('console_request', default=None)
attempt_context = contextvars.ContextVar('console_attempt', default=None)
INFERENCE = {'/v1/chat/completions', '/v1/responses', '/v1/messages'}
ATTEMPT_COLUMNS = {
    'requested_reasoning_effort':'TEXT', 'reasoning_effort':'TEXT', 'reasoning_source':'TEXT',
    'cache_hit_tokens':'INTEGER', 'cache_miss_tokens':'INTEGER', 'cache_measured':'INTEGER DEFAULT 0',
    'client_key_id':'TEXT', 'client_key_name':'TEXT', 'connection_status':'TEXT',
    'business_code':'TEXT',
}
REQUEST_COLUMNS = {'client_key_id':'TEXT', 'client_key_name':'TEXT',
                   'requested_reasoning_effort':'TEXT', 'reasoning_effort':'TEXT', 'reasoning_source':'TEXT'}


def number(value):
    return value if type(value) in (int, float) and 0 <= value <= 9223372036854775807 and math.isfinite(value) else None


def safe_text(value, limit=100):
    return str(value)[:limit] if value is not None else None


def cache_ratio(hit, total):
    return hit/total if hit is not None and total is not None and total>0 and hit<=total else None


def effort_metadata(body, original=None, default=None):
    """Record declared settings, never infer effort from model names or generated text."""
    for source in (body, original):
        if not isinstance(source,dict): continue
        effort = source.get('reasoning_effort')
        reasoning = source.get('reasoning')
        if effort is None and isinstance(reasoning,dict): effort = reasoning.get('effort')
        if isinstance(effort,str) and effort and len(effort)<=40:
            return {'requested_reasoning_effort':effort,'reasoning_effort':effort,'reasoning_source':'requested'}
        thinking = source.get('thinking')
        if isinstance(thinking,dict) and thinking.get('type') in ('enabled','disabled','adaptive'):
            # Anthropic thinking is not an OpenAI effort level: keep its declared mode.
            value = 'thinking:'+thinking['type']
            return {'requested_reasoning_effort':value,'reasoning_effort':value,'reasoning_source':'requested'}
    return {'requested_reasoning_effort':None,'reasoning_effort':safe_text(default,40),
            'reasoning_source':'default' if default else 'unknown'}


def normalized_usage(usage):
    """Project counters only, preserving absent measurements and explicit zero."""
    result = {}
    for target, aliases in {'prompt_tokens':('prompt_tokens','input_tokens'),
                            'completion_tokens':('completion_tokens','output_tokens'),
                            'credit':('credit',),
                            'cache_hit_tokens':('prompt_cache_hit_tokens','cache_read_input_tokens'),
                            'cache_miss_tokens':('prompt_cache_miss_tokens',)}.items():
        for name in aliases:
            value = number(usage.get(name))
            if value is not None:
                result[target] = value
                break
    if 'cache_hit_tokens' not in result:
        for name in ('prompt_tokens_details','input_tokens_details'):
            details = usage.get(name)
            value = number(details.get('cached_tokens')) if isinstance(details,dict) else None
            if value is not None:
                result['cache_hit_tokens'] = value
                break
    # Anthropic input_tokens excludes read/creation tokens; OpenAI prompt/input includes them.
    anthropic = number(usage.get('cache_read_input_tokens')) is not None
    if anthropic and number(usage.get('prompt_tokens')) is None and number(usage.get('input_tokens')) is not None:
        result['prompt_tokens'] = usage['input_tokens'] + usage['cache_read_input_tokens'] + (number(usage.get('cache_creation_input_tokens')) or 0)
    result['cache_measured'] = 'cache_hit_tokens' in result
    if not anthropic and 'cache_miss_tokens' not in result:
        prompt, hit = result.get('prompt_tokens'), result.get('cache_hit_tokens')
        if prompt is not None and hit is not None and hit<=prompt:
            result['cache_miss_tokens'] = prompt-hit
    return result


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.error = False
        self.last_prune = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA max_page_count=32768;
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, ts REAL NOT NULL, model TEXT, route TEXT,
                status INTEGER, duration_ms REAL, outcome TEXT);
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, ts REAL NOT NULL,
                model TEXT, account TEXT, protocol TEXT, status INTEGER, duration_ms REAL,
                prompt_tokens INTEGER, completion_tokens INTEGER, credit REAL, complete INTEGER);
            CREATE INDEX IF NOT EXISTS attempts_request ON attempts(request_id);
            CREATE INDEX IF NOT EXISTS requests_time ON requests(ts);
        ''')
        # ALTER only missing columns; old rows, IDs and indexes stay in place.
        with self.db:
            for table, columns in (('requests',REQUEST_COLUMNS),('attempts',ATTEMPT_COLUMNS)):
                present = {r['name'] for r in self.db.execute('PRAGMA table_info('+table+')')}
                for name, kind in columns.items():
                    if name not in present: self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
        self.started_at = self.db.execute('SELECT min(ts) FROM requests').fetchone()[0] or time.time()

    def write(self, sql, values):
        with self.lock:
            try:
                with self.db:
                    self.db.execute(sql, values)
                    now = time.time()
                    if now - self.last_prune > 3600:
                        self.db.execute('DELETE FROM attempts WHERE ts<?', (now-30*86400,))
                        self.db.execute('DELETE FROM requests WHERE ts<?', (now-30*86400,))
                        self.last_prune = now
                self.error = False
                return True
            except sqlite3.Error:
                self.error = True
                logging.getLogger('console').error('Console ledger write failed')
                return False

    def add_request(self, row):
        self.write('''INSERT OR REPLACE INTO requests
                   (request_id,ts,model,route,status,duration_ms,outcome,client_key_id,client_key_name,
                    requested_reasoning_effort,reasoning_effort,reasoning_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (row['request_id'], row.get('ts', time.time()), str(row.get('model') or '')[:150],
                    row.get('route', '')[:100], row.get('status', 500), row.get('duration_ms', 0),
                    row.get('outcome', 'completed'), safe_text(row.get('client_key_id')),
                    safe_text(row.get('client_key_name')), safe_text(row.get('requested_reasoning_effort'),40),
                    safe_text(row.get('reasoning_effort'),40),safe_text(row.get('reasoning_source'),20)))

    def add_attempt(self, row):
        fields = {'request_id':safe_text(row['request_id']), 'ts':row.get('ts',time.time()),
                  'model':safe_text(row.get('model') or '',150),'account':safe_text(row.get('account') or 'unknown',200),
                  'protocol':safe_text(row.get('protocol') or '',30),'status':row.get('status',502),
                  'duration_ms':number(row.get('duration_ms',0)), 'complete':int(bool(row.get('complete')))}
        for name in ('prompt_tokens','completion_tokens','credit','cache_hit_tokens','cache_miss_tokens'):
            fields[name] = number(row.get(name))
        fields['cache_measured'] = int(fields['cache_hit_tokens'] is not None)
        for name in ATTEMPT_COLUMNS:
            if name not in fields: fields[name] = safe_text(row.get(name), 100)
        fields['connection_status'] = fields['connection_status'] or 'unknown'
        columns = ','.join(fields)
        if self.write('INSERT INTO attempts ('+columns+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values())):
            return {**fields,'cache_hit_ratio':cache_ratio(fields['cache_hit_tokens'], fields['prompt_tokens'])}

    def summary(self):
        # Beijing calendar days, independent of the host process timezone.
        today = ((time.time()+28800)//86400)*86400-28800
        with self.lock:
            r = self.db.execute('''SELECT count(*) requests,
                coalesce(sum(CASE WHEN status>=200 AND status<300 AND outcome='completed'
                    AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.request_id=r.request_id AND a.status=200 AND a.complete=0)
                    THEN 1 ELSE 0 END),0) successful
                FROM requests r WHERE ts>=?''', (today,)).fetchone()
            a = self.db.execute('''SELECT count(*) attempts,coalesce(sum(credit),0) credit,
                coalesce(sum(CASE WHEN credit IS NULL THEN 1 ELSE 0 END),0) unknown_attempts,
                coalesce(sum(prompt_tokens),0) prompt_tokens,coalesce(sum(completion_tokens),0) completion_tokens,
                sum(cache_hit_tokens) cache_hit_tokens,sum(cache_miss_tokens) cache_miss_tokens,
                sum(CASE WHEN cache_hit_tokens<=prompt_tokens THEN prompt_tokens ELSE NULL END) cache_measured_input_tokens,
                sum(CASE WHEN cache_hit_tokens<=prompt_tokens THEN cache_hit_tokens ELSE NULL END) cache_ratio_hit_tokens,
                coalesce(sum(CASE WHEN cache_measured=0 THEN 1 ELSE 0 END),0) cache_unknown_attempts,
                coalesce(sum(cache_measured),0) cache_measured_attempts
                FROM attempts WHERE ts>=?''', (today,)).fetchone()
            counters = dict(a)
            counters['cache_hit_ratio'] = cache_ratio(counters.pop('cache_ratio_hit_tokens'), counters['cache_measured_input_tokens'])
            counters['cache_measured'] = bool(counters['cache_measured_attempts'])
            return {**dict(r), **counters, 'started_at': self.started_at, 'write_error': self.error,
                    'retention_days':30}

    def requests(self, page=1, model=''):
        page = min(max(int(page),1),100000)
        where, params = ('WHERE r.model=?', [model]) if model else ('', [])
        with self.lock:
            total=self.db.execute('SELECT count(*) FROM requests r '+where,params).fetchone()[0]
            rows=self.db.execute('''SELECT r.*,count(a.id) attempt_count,sum(a.credit) credit,
                sum(CASE WHEN a.id IS NOT NULL AND a.credit IS NULL THEN 1 ELSE 0 END) unknown_attempts,
                sum(a.prompt_tokens) prompt_tokens,sum(a.completion_tokens) completion_tokens,
                sum(a.cache_hit_tokens) cache_hit_tokens,sum(a.cache_miss_tokens) cache_miss_tokens,
                sum(CASE WHEN a.cache_hit_tokens<=a.prompt_tokens THEN a.prompt_tokens ELSE NULL END) cache_measured_input_tokens,
                sum(CASE WHEN a.cache_hit_tokens<=a.prompt_tokens THEN a.cache_hit_tokens ELSE NULL END) cache_ratio_hit_tokens,
                max(a.cache_measured) cache_measured,
                group_concat(DISTINCT a.account) accounts,
                max(CASE WHEN a.status=200 AND a.complete=0 THEN 1 ELSE 0 END) incomplete
                FROM requests r LEFT JOIN attempts a ON a.request_id=r.request_id '''+where+'''
                GROUP BY r.request_id ORDER BY r.ts DESC LIMIT 50 OFFSET ?''',
                params+[(page-1)*50]).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                item['cache_hit_ratio']=cache_ratio(item.pop('cache_ratio_hit_tokens'),item['cache_measured_input_tokens'])
                item['cache_measured']=bool(item['cache_measured'])
                if item.pop('incomplete') or item['status']>=400:item['outcome']='incomplete' if item['status']<400 else 'failed'
                item['attempts']=[dict(a) for a in self.db.execute('''SELECT model,account,protocol,status,
                    duration_ms,prompt_tokens,completion_tokens,credit,complete,'''+','.join(ATTEMPT_COLUMNS)+'''
                    FROM attempts WHERE request_id=? ORDER BY id''',(item['request_id'],)).fetchall()]
                for attempt in item['attempts']:
                    attempt['cache_hit_ratio']=cache_ratio(attempt['cache_hit_tokens'],attempt['prompt_tokens'])
                    attempt['cache_measured']=bool(attempt['cache_measured'])
                result.append(item)
            return {'rows':result,'total':total,'page':page,'page_size':50}


class UsageStream(httpx.AsyncByteStream):
    def __init__(self, source, ledger, metadata, on_result=None):
        self.source, self.ledger, self.metadata = source, ledger, dict(metadata)
        self.on_result = on_result
        self.started = time.monotonic()
        self.pending = b''
        self.dropping = False
        self.usage = {}
        self.done = False
        self.failed = False
        self.saved = False
        self.reported_usage = {}

    def feed(self, chunk):
        # Only one bounded SSE line is retained in memory; no content is persisted.
        for fragment in chunk.splitlines(keepends=True):
            if len(self.pending)+len(fragment)>262144:
                self.pending=b''; self.dropping=True
            if not self.dropping:self.pending+=fragment
            if fragment.endswith((b'\n',b'\r')):
                if not self.dropping:self.line(self.pending.strip())
                self.pending=b''; self.dropping=False

    def line(self, line):
        value=line[5:].strip() if line.startswith(b'data:') else line
        if value==b'[DONE]':self.done=True;return
        try:row=json.loads(value)
        except (ValueError,UnicodeError):return
        if not isinstance(row,dict):return
        code = row.get('code')
        if row.get('error'):
            self.failed=True
            error = row['error']
            code = error.get('code') if isinstance(error,dict) else row.get('code')
        if type(code) in (int,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}',str(code)):
            self.metadata['business_code']=str(code)
        if row.get('type') in ('response.completed','message_stop'): self.done=True
        choices=row.get('choices')
        if isinstance(choices,list) and any(isinstance(choice,dict) and choice.get('finish_reason') in ('stop','length','tool_calls','function_call','content_filter')
               for choice in choices):
            self.done=True
        containers = [row]
        containers.extend(row[key] for key in ('response','message') if isinstance(row.get(key),dict))
        for container in containers:
            usage=container.get('usage')
            if isinstance(usage,dict):
                # Restrict the in-memory projection too; never retain response text.
                for key in ('prompt_tokens','completion_tokens','input_tokens','output_tokens','credit',
                            'prompt_cache_hit_tokens','prompt_cache_miss_tokens','cache_read_input_tokens',
                            'cache_creation_input_tokens'):
                    value=number(usage.get(key))
                    if value is not None:self.reported_usage[key]=value
                for key in ('prompt_tokens_details','input_tokens_details'):
                    details=usage.get(key)
                    value=number(details.get('cached_tokens')) if isinstance(details,dict) else None
                    if value is not None:self.reported_usage[key]={'cached_tokens':value}
                self.usage=normalized_usage(self.reported_usage)
        if not line.startswith(b'data:') and not row.get('error'):
            self.done = True

    def save(self):
        if self.saved:return
        self.saved=True
        if self.pending:self.line(self.pending)
        result = self.ledger.add_attempt({**self.metadata,**self.usage,
            'duration_ms':round((time.monotonic()-self.started)*1000,2),
            'complete':self.done and not self.failed})
        if result is not None and self.on_result is not None:
            try:self.on_result(result)
            except Exception:logging.getLogger('console').error('Console result callback failed')

    async def __aiter__(self):
        try:
            async for chunk in self.source:
                self.feed(chunk)
                yield chunk
        except BaseException as exc:
            # Responses/Messages adapters close the upstream iterator after its
            # semantic finish frame instead of consuming the trailing [DONE].
            if isinstance(exc,GeneratorExit) and self.done:raise
            self.failed=True
            if isinstance(exc,(asyncio.CancelledError,GeneratorExit)):
                self.metadata['connection_status']='cancelled'
            else:
                self.metadata['connection_status']='timeout' if isinstance(exc,httpx.TimeoutException) else 'connection_error'
            raise
        finally:self.save()

    async def aclose(self):
        try:await self.source.aclose()
        finally:self.save()


class RequestLedger:
    def __init__(self, app, ledger):self.app,self.ledger=app,ledger

    async def __call__(self, scope, receive, send):
        if scope['type']!='http' or scope.get('path') not in INFERENCE or scope.get('method')!='POST':
            return await self.app(scope,receive,send)
        row={'request_id':uuid.uuid4().hex,'ts':time.time(),'route':scope['path'],'status':500,'outcome':'incomplete'}
        owner=scope.get('client_key') or client_key_context.get()
        if owner:
            row.update(client_key_id=owner.get('id'),client_key_name=owner.get('name'))
        token=request_context.set(row)
        start=time.monotonic()
        async def observe(message):
            if message['type']=='http.response.start':
                row['status']=message['status']
                message={**message,'headers':list(message.get('headers',[]))+[(b'x-workbuddy-request-id',row['request_id'].encode())]}
            await send(message)
            if message['type']=='http.response.body' and not message.get('more_body',False):
                row['outcome']='completed' if row['status']<400 else 'failed'
        try:await self.app(scope,receive,observe)
        finally:
            row['duration_ms']=round((time.monotonic()-start)*1000,2)
            self.ledger.add_request(row)
            request_context.reset(token)


def install_observation(proxy, ledger, on_result=None, effort_default=None):
    """Wrap the pinned upstream's inference client; leave its files untouched."""
    original_httpx=proxy.httpx
    original_forward=proxy._forward_chat_once

    async def forward(body,protocol,original,state):
        request=request_context.get()
        if request is None:return await original_forward(body,protocol,original,state)
        request['model']=str(body.get('model') or '')[:150]
        default=None
        if effort_default is not None:
            try:default=effort_default(request['model'])
            except Exception:pass
        effort=effort_metadata(body,original,default)
        request.update(effort)
        owner=client_key_context.get()
        if owner:
            request.update(client_key_id=owner.get('id'),client_key_name=owner.get('name'))
        manager=getattr(state,'account_runtime',None)
        account=manager.account_id_for_client(state.client) if manager else 'unknown'
        token=attempt_context.set({'request_id':request['request_id'],'model':request['model'],
                                   'account':account,'protocol':protocol,'ts':time.time(),**effort,
                                   'client_key_id':request.get('client_key_id'),
                                   'client_key_name':request.get('client_key_name')})
        try:return await original_forward(body,protocol,original,state)
        finally:attempt_context.reset(token)

    class ObservedClient(original_httpx.AsyncClient):
        async def send(self, request, **kwargs):
            metadata=attempt_context.get()
            if metadata is None:return await super().send(request,**kwargs)
            start=time.monotonic()
            try:response=await super().send(request,**kwargs)
            except Exception as exc:
                timeout=isinstance(exc,httpx.TimeoutException)
                row=ledger.add_attempt({**metadata,'status':504 if timeout else 502,
                    'duration_ms':(time.monotonic()-start)*1000,
                    'connection_status':'timeout' if timeout else 'connection_error'})
                if row is not None and on_result is not None:
                    try:on_result(row)
                    except Exception:logging.getLogger('console').error('Console result callback failed')
                raise
            stream=UsageStream(response.stream,ledger,{**metadata,'status':response.status_code,
                               'connection_status':'connected'},on_result=on_result)
            if response.is_closed:
                stream.feed(response.content);stream.save()
            else:response.stream=stream
            return response

    proxy.httpx=types.SimpleNamespace(**{**vars(original_httpx),'AsyncClient':ObservedClient})
    proxy._forward_chat_once=forward
