"""Read-only storage adapters. They know storage fields, not physical meaning."""
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlencode,urlsplit
from urllib.request import build_opener,ProxyHandler

MAX_BYTES=8*1024*1024
MAX_POINTS=20000


def path_value(payload,path):
    for part in path.split('.'):
        if not isinstance(payload,dict):return None
        payload=payload.get(part)
    return payload


def validate_source(kind,config):
    if not isinstance(config,dict):raise ValueError('Metric source must be a mapping')
    if kind=='metrics.victoriametrics':
        if set(config)!={'endpoint'}:raise ValueError('Expected metric endpoint')
        u=urlsplit(config['endpoint'])
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('Invalid metric endpoint')
    elif kind=='metrics.sqlite':
        if set(config)!={'path'} or not isinstance(config['path'],str) or not Path(config['path']).is_absolute():raise ValueError('Expected absolute telemetry database path')
    else:raise ValueError('Unknown metric driver')


def validate_query(kind,q):
    keys={'selector','quality_selector'} if kind=='metrics.victoriametrics' else {'run_id','kind','value_field','time_field','quality_field'}
    required={'selector'} if kind=='metrics.victoriametrics' else {'run_id','kind','value_field','time_field'}
    if not isinstance(q,dict) or set(q)-keys or not required<=set(q):raise ValueError('Invalid metric query fields')
    if any(not isinstance(v,str) or not v or len(v)>4096 for v in q.values()):raise ValueError('Invalid metric query value')


class VictoriaMetrics:
    def __init__(self,config):self.config=config
    def export(self,selector,start,end):
        url=self.config['endpoint'].rstrip('/')+'/api/v1/export?'+urlencode({'match[]':selector,'start':start,'end':end})
        with build_opener(ProxyHandler({})).open(url,timeout=10) as response:raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('Metric export exceeds byte limit')
        series={};count=0
        for line in raw.splitlines():
            r=json.loads(line);labels=r['metric'];values=r['values'];times=r['timestamps']
            if not isinstance(labels,dict) or len(values)!=len(times):raise ValueError('Malformed metric export')
            key=json.dumps(labels,sort_keys=True);points=series.setdefault(key,{})
            for stamp,value in zip(times,values):
                if type(stamp) not in (int,float) or not math.isfinite(stamp):raise ValueError('Invalid metric timestamp')
                t=stamp/1000
                if t in points and points[t]!=value:raise ValueError('Conflicting metric samples')
                points[t]=value;count+=1
                if count>MAX_POINTS:raise ValueError('Metric export exceeds sample limit')
        if len(series)>1:raise ValueError('Ambiguous metric selector: expected one series')
        return next(iter(series.values()),{})
    def read(self,query,start,end):
        values=self.export(query['selector'],start,end)
        quality=self.export(query['quality_selector'],start,end) if query.get('quality_selector') else {}
        return [dict(time=t,value=v,quality=quality.get(t)) for t,v in sorted(values.items()) if start<=t<=end]


class TelemetrySQLite:
    def __init__(self,config):self.config=config
    def read(self,query,start,end):
        uri=Path(self.config['path']).as_uri()+'?mode=ro'
        with sqlite3.connect(uri,uri=True) as db:
            # Values and all field names are parameters or Python lookups, never SQL fragments.
            rows=db.execute('SELECT payload_json FROM telemetry WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT ?',
                            (query['run_id'],query['kind'],MAX_POINTS+1)).fetchall()
        if len(rows)>MAX_POINTS:raise ValueError('SQLite query exceeds sample limit; select a narrower run/kind')
        result=[]
        for row in rows:
            payload=json.loads(row[0]);stamp=path_value(payload,query['time_field'])
            if type(stamp) not in (int,float) or not math.isfinite(stamp):raise ValueError('Missing acquisition timestamp in metric payload')
            if start<=stamp<=end:
                result.append(dict(time=stamp,value=path_value(payload,query['value_field']),
                                   quality=path_value(payload,query['quality_field']) if query.get('quality_field') else None))
        return sorted(result,key=lambda p:p['time'])


DRIVERS={'metrics.victoriametrics':VictoriaMetrics,'metrics.sqlite':TelemetrySQLite}
