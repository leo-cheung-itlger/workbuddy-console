"""Manual, attributed benchmark snapshots; no scheduled benchmark fetches."""
import asyncio
import csv
from datetime import date
import io
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

SOURCES=('livebench.ai','livebench.github.io','artificialanalysis.ai')


def score(value):
    if value is None:return None
    if type(value) not in (float,int) or not math.isfinite(value) or not 0<=value<=100:raise ValueError('评分需为0–100')
    return float(value)


class Benchmarks:
    def __init__(self,root):self.path=Path(root)/'benchmarks.json'

    def snapshot(self):
        try:return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return {'records':[],'updated_at':None}

    def import_records(self,data):
        records=data.get('records')
        if not isinstance(records,list) or len(records)>500:raise ValueError('最多导入500条评分')
        safe=[]
        for row in records:
            if not isinstance(row,dict):raise ValueError('评分格式无效')
            identifier=row.get('model_id');source=row.get('source_url','');parts=urlsplit(source)
            if (not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}',identifier)
                    or parts.scheme!='https' or parts.hostname not in SOURCES or parts.username or parts.password):
                raise ValueError('需精确模型ID及官方来源链接')
            stamp=str(row.get('date',''));date.fromisoformat(stamp)
            safe.append({'model_id':identifier,'benchmark_model':str(row.get('benchmark_model') or identifier)[:200],
                'reasoning':str(row.get('reasoning') or '来源未注明')[:80],'source':str(row.get('source') or parts.hostname)[:80],
                'source_url':source[:500],'date':stamp,'coding':score(row.get('coding')),
                'agentic':score(row.get('agentic')),'general':score(row.get('general'))})
        result={'records':safe,'updated_at':time.time()}
        temp=self.path.with_suffix('.tmp')
        with open(temp,'w',encoding='utf-8') as stream:
            os.chmod(temp,0o600);json.dump(result,stream,ensure_ascii=False)
        os.replace(temp,self.path)
        return result

    async def update_livebench(self):
        # Choose the latest dated official public dataset; never scrape third-party rankings.
        async with httpx.AsyncClient(timeout=20,trust_env=False,headers={'User-Agent':'WorkBuddy-Console'}) as client:
            response=await client.get('https://api.github.com/repos/LiveBench/livebench.github.io/contents/public')
            response.raise_for_status()
            dates=[]
            for row in response.json():
                match=re.fullmatch(r'table_(\d{4}_\d{2}_\d{2})\.csv',row.get('name',''))
                if match:dates.append(match[1])
            if not dates:raise ValueError('官方评分数据暂不可用')
            latest=max(dates);base='https://raw.githubusercontent.com/LiveBench/livebench.github.io/main/public/'
            table,categories=await asyncio.gather(client.get(base+'table_'+latest+'.csv'),
                                                 client.get(base+'categories_'+latest+'.json'))
            table.raise_for_status();categories.raise_for_status()
            if len(table.content)>2*1024*1024:raise ValueError('评分数据过大')
            groups=categories.json();records=[]
            for row in csv.DictReader(io.StringIO(table.text)):
                name=row.get('model','')
                if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}',name):continue
                def mean(fields):
                    values=[float(row[k]) for k in fields if row.get(k) not in ('',None,'NaN')]
                    return sum(values)/len(values) if values else None
                averages=[mean(fields) for fields in groups.values()]
                records.append({'model_id':name,'benchmark_model':name,'reasoning':'来源模型名所示，未另行推定',
                    'source':'LiveBench','source_url':'https://livebench.github.io/', 'date':latest.replace('_','-'),
                    'coding':mean(groups.get('Coding',[])),'agentic':mean(groups.get('Agentic Coding',[])),
                    'general':sum(v for v in averages if v is not None)/sum(v is not None for v in averages)})
            # Exact IDs only. Different snapshots/efforts are available for manual explicit mapping.
            return self.import_records({'records':records})

    def enrich(self,models,observed):
        records=self.snapshot()['records'];by_id={r['model_id']:r for r in records}
        output=[]
        for model in models:
            row=dict(model);benchmark=by_id.get(row['id']);cost=observed.get(row['id'])
            row['benchmark']=benchmark
            values=[benchmark[k] for k in ('coding','agentic') if benchmark and benchmark.get(k) is not None]
            row['recommendation_score']=round(sum(values)/len(values),2) if values else None
            row['cost_observation']=None;row['value_score']=None
            if cost and cost.get('samples',0)>=3 and cost.get('tokens',0)>0:
                rate=cost['credit']/cost['tokens']*1000000
                row['cost_observation']={'samples':cost['samples'],'credits_per_million_tokens':round(rate,4)}
                if rate>0 and row['recommendation_score'] is not None:
                    row['value_score']=round(row['recommendation_score']/rate,4)
            output.append(row)
        return sorted(output,key=lambda r:(r['recommendation_score'] is None,-(r['recommendation_score'] or 0),r['id']))
