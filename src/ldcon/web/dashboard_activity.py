"""Display-only activity lanes from bounded, existing host publications.

No control/API lifecycle reads. A running agent task means work in progress,
not proof of a physical adjustment. Monitor liveness uses its own heartbeat,
not progress.json (which changes only when the monitor phase changes).
"""
import time
from ldcon.web.dashboard_snapshot import ID, TERMINAL, read, root_for
from ldcon.web.dashboard_state import fresh

LABELS = {
 0:('IDLE','gray'),1:('UNKNOWN / STALE','gray'),
 10:('MONITOR OFF','gray'),11:('MONITORING','green'),12:('REVIEW / TASK IN PROGRESS','blue'),
 13:('WAITING FOR AGENT','yellow'),14:('NEEDS INSPECTION','orange'),
 15:('MONITOR DATA UNAVAILABLE','orange'),16:('MONITOR DISABLED','gray'),
 17:('MONITOR NOT CONFIGURED','orange'),18:('STOPPING MONITOR','yellow'),
 20:('AGENT TASK IN PROGRESS','blue'),21:('ONLINE TUNING / WINDOW SEARCH','purple'),
 22:('RECOVERY / REPLANNING','orange'),23:('RESTORING / CANCELLING','yellow'),
 30:('TARGET ACCEPTED / QUEUED','purple'),31:('CHANGING TARGET','blue'),
 32:('TARGET SETTLING','blue'),33:('STOPPING TARGET CHANGE','yellow'),
 40:('PREPARING SWEEP','purple'),41:('SWEEP ARMED','purple'),42:('SWEEPING','blue'),
 43:('RESTORING / STOPPING SWEEP','yellow'),
 50:('STUDY / OPERATING-POINT WORK','blue'),51:('MULTIPLE OPERATIONS','purple'),
}
LANES={'monitor':'Monitor','adjustment':'Adjustment','target':'Target change','sweep':'Sweep','other':'Other work'}
MONITOR={
 'watching':11,'waiting_task':12,'review_started':12,'admitting_task':12,
 'review_unresolved':13,'waiting_resident':13,'task_requires_inspection':14,
 'control_data_unavailable':15,'disabled':16,'unconfigured':17,
 'task_unavailable':15,'admission_unconfirmed':1,
}
PASSIVE={'agent.resident','agent.request','agent.log_watch','monitor.wavelength','acquire.metrics','notify.teams','report.grafana_dashboard'}

def mappings():
 return [dict(type='value',options={str(k):dict(text=v[0],color=v[1]) for k,v in LABELS.items()})]

def activity(jobs, *, now=None):
 now=time.time() if now is None else now
 values={lane:[] for lane in LANES}
 for j in jobs:
  if j.get('status') in TERMINAL:continue
  ops=[c.get('operation',c.get('card','')) for c in j.get('cards',[])]
  verified=j.get('worker_attachment')=='attached'
  for op in ops:
   if op=='agent.monitor':
    m=j.get('monitor_activity',{})
    code=MONITOR.get(m.get('state'),1) if verified and fresh(m.get('updated_unix_s'),now) else 1
    if code!=1 and j.get('status') in ('stopping','finishing'):code=18
    values['monitor'].append(code);continue
   if op.startswith('servo.'):
    if not verified:continue
    online=j.get('online_tuning') or {}
    if online.get('status') in ('queued','running','restoring','stopping'):
     values['adjustment'].append(23 if online['status'] in ('restoring','stopping') else 21)
    continue
   if op in PASSIVE:continue
   lane=('adjustment' if op in ('agent.task','agent.history_review') else
         'target' if op=='tune.lock_target' else 'sweep' if op.startswith('sweep.') else 'other')
   # Most long model turns do not update progress, so worker identity establishes
   # process presence. Their existence is not reported as an owner physical move.
   if not verified:code=1
   elif lane=='adjustment':code=23 if j.get('status') in ('stopping','finishing') else 20
   elif lane=='target':
    state=(j.get('lock_target') or {}).get('state')
    code=33 if j.get('status') in ('stopping','finishing') else 30 if j.get('status') in ('queued','starting') else 32 if state in ('settling','verifying') else 31
   elif lane=='sweep':
    s=j.get('shot_sweep') or {};state=s.get('state')
    code=(43 if j.get('status') in ('stopping','finishing') or state in ('stopping','cancelling','restoring','restored','settling','done') else
          42 if state in ('fired','running','playing') else 41 if state in ('armed','ready') and s.get('armed') else
          40 if state in ('queued','arming','preparing','accepted') or not s and j.get('status') in ('queued','starting') else 1)
    if not fresh(j.get('updated_utc'),now):code=1
   else:code=50
   values[lane].append(code)
 result={}
 for lane,states in values.items():
  # Concurrent lanes never hide each other. Conflict within one lane is explicit.
  unique=set(states)
  result[lane+'_activity_code']=next(iter(unique)) if len(unique)==1 else (1 if 1 in unique else 51) if unique else 10 if lane=='monitor' else 0
 return result

def collect(service,laser_id,jobs,*,now=None):
 now=time.time() if now is None else now;root=root_for(service)
 values=activity(jobs,now=now)
 accepted=[];queued=False;unconfirmed=False
 for area,pattern in (('lock-target-api','changes/*.json'),('lock-target-queue','*.json')):
  directory=root/area/laser_id
  for path in directory.glob(pattern):
   row=read(root,path,{})
   if not isinstance(row,dict):continue
   stamp=row.get('created_unix_s')
   if type(stamp) in (int,float) and 0<stamp<=now:accepted.append(stamp)
   if area=='lock-target-queue':
    queued|=row.get('phase')=='queued'
    unconfirmed|=row.get('phase') in ('dispatching','unknown')
 # A queued intent is not evidence of a target write. Existing cards retain their
 # own live lane; expired/unknown dispatches remain explicit until reconciled.
 if unconfirmed:values['target_activity_code']=1
 elif queued and values['target_activity_code']==0:values['target_activity_code']=30
 values['target_last_accepted_unix_s']=max(accepted,default=0.)
 return values

def query(lane,selector='laser!="",simulation="false"'):
 if lane not in LANES:raise ValueError('Unknown activity lane')
 metric='ldcon_'+lane+'_activity_code{'+selector+',producer="dashboard-metadata",kind="dashboard_metadata"}'
 health='ldcon_metadata_read_ok{'+selector+',producer="dashboard-metadata",kind="dashboard_metadata"}'
 raw='(last_over_time('+metric+'[25s]) and (tlast_over_time('+metric+'[25s]) == tlast_over_time('+health+'[25s])) and (last_over_time('+health+'[25s]) == 1))'
 from ldcon.web.metric_query_profiles import fleet_inventory_query
 known=fleet_inventory_query(selector)
 return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1)) or on(laser) (1 + 0 * '+known+')'
