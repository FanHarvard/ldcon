"""Independent read-only dashboard metadata collector; no control/owner methods.

Discover lasers through the fleet registry. Publish the configured optical target
and ongoing operation separately from the existing physical lock-state metrics.
Control metadata reads bounded published files. Independent display wavelengths poll
the configured wavemeter cached acquisition API; no control owner is contacted.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import time
from datetime import datetime
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.core.telegraf import TelegrafExporter
from ldcon.web.web_fleet import read_registry, NoRedirect

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
PASSIVE = {'agent.resident','agent.log_watch','agent.request','monitor.wavelength','acquire.metrics','notify.teams','report.grafana_dashboard'}
TERMINAL = {'succeeded','completed','completed_with_failures','completed_unsettled','failed','cancelled','interrupted','inconclusive'}
CODES = {'idle':0,'sweeping':14,'sweep_armed':15,'target_change':16,'operating_point':17,'study':18,'operation_unknown':19,'preparing_sweep':20,'stopping_sweep':21}


def get(service, route):
    # This collector has no generic HTTP method or arbitrary URL entry point.
    if not re.fullmatch(r'sets|jobs\?active_only=true|lasers/[A-Za-z0-9_.-]+/target',route):
        raise ValueError('Unsupported read-only metadata route')
    req=Request(service.origin+service.capability_prefix()+'/api/'+route,method='GET',headers={'X-LDCon-Client':'web'})
    with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=4) as response:
        raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('Metadata response too large')
    return json.loads(raw)


def operation(jobs, now=None):
    now=time.time() if now is None else now
    values=[]
    for j in jobs:
        if j.get('status') in TERMINAL or j.get('read_only'):continue
        try:age=now-datetime.fromisoformat(j['updated_utc']).timestamp()
        except (ValueError,KeyError,TypeError):age=math.inf
        for card in j.get('cards',[]):
            op=card.get('operation',card.get('card',''))
            if op.startswith('servo.') or op in PASSIVE:continue
            if not 0<=age<=25:values.append(CODES['operation_unknown']);continue
            if op.startswith('sweep.'):
                # Read the worker's published record, never sweep facade GETs:
                # facade status reads also run its lifecycle ticker (writes).
                s=j.get('shot_sweep') or {}
                state=s.get('state')
                values.append(CODES['stopping_sweep'] if j.get('status') in ('stopping','finishing') or state in ('stopping','cancelling','restoring','restored','settling','done') else
                              CODES['sweeping'] if state in ('fired','running','playing') else
                              CODES['sweep_armed'] if state in ('armed','ready') and s.get('armed') else
                              CODES['preparing_sweep'] if state in ('queued','arming','preparing','accepted') or not s and j.get('status') in ('starting','queued') else
                              CODES['operation_unknown'])
            elif op=='tune.lock_target':values.append(CODES['target_change'])
            elif op=='tune.operating_point':values.append(CODES['operating_point'])
            elif op.startswith(('study.','tune.','commission.','acquisition.')):values.append(CODES['study'])
            else:values.append(CODES['operation_unknown'])
    return values[0] if len(values)==1 else CODES['operation_unknown'] if values else 0


def collect(service, reader=None):
    # The production path reads existing bounded worker/config publications.
    # It performs no HTTP request, SQLite query, or control-process mutation.
    from ldcon.web.dashboard_snapshot import inventory, snapshot
    if reader is None:
        sets=inventory(service);jobs=snapshot(service)['jobs']
    else:
        sets=reader(service,'sets')['sets'];jobs=reader(service,'jobs?active_only=true')['jobs']
    results=[]
    for laser in sets:
        ident=laser.get('id');name=laser.get('name')
        if not isinstance(ident,str) or not ID.fullmatch(ident) or not isinstance(name,str):continue
        row={'metadata_read_ok':1}
        from ldcon.web.quality_threshold_metrics import collect as collect_quality, extract
        row.update(collect_quality(service,ident) if reader is None else extract(laser))
        try:
            target=laser.get('target') if reader is None else reader(service,'lasers/'+ident+'/target')
            frequency=target.get('frequency_hz') if isinstance(target,dict) else None
            if type(frequency) in (int,float) and math.isfinite(frequency) and frequency>0:
                row['configured_target_frequency_hz']=frequency
                row['configured_target_wavelength_nm']=299792458e9/frequency
                row['target_valid']=1
            else:row['target_valid']=0
        except (OSError,ValueError,KeyError):row['target_valid']=0
        own=[j for j in jobs if j.get('set_id')==ident]
        row['operation_state_code']=operation(own)  # Legacy telemetry; no longer the State panel.
        from ldcon.web.dashboard_activity import collect as activities, activity
        row.update(activities(service,ident,own) if reader is None else activity(own))
        from ldcon.web.dashboard_state import display_state
        display=display_state(own)
        dither=display.get('dither_quality',{})
        row['dither_quality_code']=2 if dither.get('high') is True else 1 if dither.get('high') is False else 0
        row['dither_quality_valid_until_unix_s']=dither.get('valid_until_unix_s') or 0
        row['dither_display_state_code']=display['code'] if display['code'] in (22,23) else 0
        results.append((ident,name,row))
    return results


def collect_and_publish(service):
    from ldcon.web.dashboard_wavelength import publish
    from ldcon.web.dashboard_snapshot import root_for
    publish(root_for(service))
    return collect(service)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry',required=True)
    p.add_argument('--interval',type=float,default=5.)
    p.add_argument('--once',action='store_true')
    p.add_argument('--no-send',action='store_true')
    args=p.parse_args()
    if args.interval<2:raise ValueError('Interval must be at least 2 seconds')
    exporters={}
    try:
        while True:
            started=time.monotonic();services=read_registry(args.registry)
            endpoint=json.load(open(args.registry)).get('telemetry_endpoint')
            if not args.no_send and not endpoint:raise ValueError('Registry telemetry endpoint is not configured')
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending={pool.submit(collect_and_publish,s):s for s in services.values()}
                seen=set()
                for future,service in pending.items():
                    try:rows=future.result()
                    except (OSError,ValueError,KeyError,TypeError) as e:
                        # Do not refresh a previous target/operation with a new
                        # timestamp. Display expires after25seconds instead.
                        print(json.dumps({'service':service.id,'read_ok':False,'error_type':type(e).__name__}),flush=True)
                        continue
                    for ident,name,row in rows:
                        key=(endpoint,service.id,ident,name);seen.add(key)
                        if args.no_send:print(json.dumps({'laser':name,**row}),flush=True);continue
                        if key not in exporters:exporters[key]=TelegrafExporter(endpoint,name,simulation=False,tags={'laser_id':ident,'service_id':service.id,'producer':'dashboard-metadata'})
                        exporters[key].send('metadata',0,'dashboard_metadata',row)
                for key in set(exporters)-seen:exporters.pop(key).close()
            if args.once:return
            time.sleep(max(0,args.interval-(time.monotonic()-started)))
    finally:
        for e in exporters.values():e.close()


if __name__=='__main__':main()
