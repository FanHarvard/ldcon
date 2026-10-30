import copy
from datetime import datetime,timezone
from types import SimpleNamespace
import json
from ldcon.web.dashboard_activity import activity,collect,query
from ldcon.web.grafana_activity import apply,state_only
from ldcon.web.metric_query_profiles import fleet_status_query


def job(op,**kw):
 return dict(id='a',cards=[dict(operation=op)],status='running',worker_attachment='attached',
             updated_utc=datetime.fromtimestamp(1000,timezone.utc).isoformat(),**kw)

def test_monitor_own_heartbeat_and_independent_lanes():
 m=job('agent.monitor',monitor_activity=dict(state='watching',updated_unix_s=999))
 m['updated_utc']='2020-01-01T00:00:00Z'
 v=activity([m,job('agent.task'),job('tune.lock_target'),job('sweep.servo_modulation',shot_sweep=dict(state='armed',armed=True))],now=1000)
 assert v==dict(monitor_activity_code=11,adjustment_activity_code=20,target_activity_code=31,sweep_activity_code=41,other_activity_code=0)
 assert activity([m],now=1030)['monitor_activity_code']==1
 m['monitor_activity'].update(state='task_requires_inspection',updated_unix_s=1000)
 assert activity([m],now=1001)['monitor_activity_code']==14
 m['worker_attachment']='pending_or_unverified'
 assert activity([m],now=1001)['monitor_activity_code']==1

def test_off_disabled_stopped_and_unknown_are_distinct():
 assert activity([],now=1000)['monitor_activity_code']==10
 m=job('agent.monitor',monitor_activity=dict(state='disabled',updated_unix_s=1000))
 assert activity([m],now=1000)['monitor_activity_code']==16
 m['status']='cancelled'
 assert activity([m],now=1000)['monitor_activity_code']==10

def test_sweep_and_online_no_control_state_override():
 servo=job('servo.ecdl_steady_lock',online_tuning=dict(status='running'))
 sweep=job('sweep.servo_modulation',shot_sweep=dict(state='playing'))
 v=activity([servo,sweep],now=1001)
 assert v['adjustment_activity_code']==21 and v['sweep_activity_code']==42
 assert 'operation_state_code' not in fleet_status_query()
 assert 'dither_high' in fleet_status_query()

def test_queue_acceptance_does_not_imply_output(tmp_path):
 (tmp_path/'jobs').mkdir();s=SimpleNamespace(state_file=tmp_path/'state.json')
 d=tmp_path/'lock-target-queue'/'test';d.mkdir(parents=True)
 (d/'a.json').write_text(json.dumps(dict(phase='queued',created_unix_s=900)))
 v=collect(s,'test',[],now=1000)
 assert v['target_activity_code']==30 and v['target_last_accepted_unix_s']==900
 (d/'a.json').write_text(json.dumps(dict(phase='cancelled',created_unix_s=900)))
 v=collect(s,'test',[],now=1001)
 assert v['target_activity_code']==0 and v['target_last_accepted_unix_s']==900

def test_patch_preserves_quality_and_folder_and_is_idempotent():
 expr='WITH (\n  a = (1),\n  operation = (old_metric),\n  dither_code = (2)\n)\nx or on(laser) (operation >= 14 <= 21) or on(laser) y'
 assert 'operation' not in state_only(expr) and 'dither_code' in state_only(expr)
 d=dict(uid='ldcon-test',folderUid='keep',panels=[dict(id=1,title='State and activity',type='state-timeline',datasource={'uid':'ds'},
   gridPos=dict(x=0,y=4,w=24,h=4),targets=[dict(expr=expr.replace('old_metric','ldcon_operation_state_code'))],fieldConfig={'defaults':{}}),
   dict(id=2,title='Quality',type='timeseries',gridPos=dict(x=0,y=8,w=12,h=8),targets=[dict(expr='baseline and threshold')])])
 old=copy.deepcopy(d);v=apply(d)
 assert d==old and v['folderUid']=='keep'
 assert next(p for p in v['panels'] if p['id']==2)['targets']==d['panels'][1]['targets']
 assert v==apply(v) and len(v['panels'])==4
 timelines=[p for p in v['panels'] if p['type']=='state-timeline']
 assert sorted((p['gridPos']['x'],p['gridPos']['w']) for p in timelines)==[(0,12),(12,12)]
 legacy=copy.deepcopy(v)
 for p in legacy['panels']:
  if p['type']=='state-timeline':p['gridPos'].update(x=8 if p['title']=='Activity — timeline' else 0,w=16 if p['title']=='Activity — timeline' else 8)
 assert apply(legacy)==v
 assert all('operation_state_code' not in t['expr'] for p in v['panels'] if p['id']==1 for t in p['targets'])
 for lane in ('monitor','adjustment','target','sweep','other'):
  assert '[25s]' in query(lane) and 'count by(laser)' in query(lane)
