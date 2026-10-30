"""Non-destructive State/Activity split for provisioned detail and fleet views."""
import copy
import re
from ldcon.web.dashboard_activity import LANES, mappings, query
from ldcon.web.dashboard_state import state_mappings
from ldcon.web.metric_query_profiles import SELECTOR

DESCRIPTION=('Activity is independent of physical State. Monitor uses its own published heartbeat; '
 'NEEDS INSPECTION means automatic follow-up is paused. Agent task presence does not prove a physical move. '
 'Target acceptance is distinct from application. Sweep stages come from worker publications. '
 'Simultaneous lanes are shown independently. Display sampling is 5 s with 25 s expiry; '
 'shorter operations can be absent from the timeline. Last target acceptance retains its recorded timestamp. '
 'No owner command or lifecycle API is used by this display.')


def state_only(expr):
 # Preserve all existing optical/dither precedence and configured references.
 expr=re.sub(r'^  operation = .*\n','',expr,flags=re.M)
 return expr.replace(' or on(laser) (operation >= 14 <= 21)','')


def panels(ds, selector, *, fleet, next_id, y, h):
 columns={chr(65+i):label for i,label in enumerate(LANES.values())}
 targets=[dict(refId=ref,expr=query(lane,selector),datasource=ds,instant=True,range=False,format='table')
          for ref,lane in zip(columns,LANES)]
 scope=selector+',producer="dashboard-metadata",kind="dashboard_metadata"'
 last='max by(laser) (last_over_time(ldcon_target_last_accepted_unix_s{'+scope+'}[25s]) > 0) * 1000'
 targets.append(dict(refId='F',expr=last,datasource=ds,instant=True,range=False,format='table'))
 columns['F']='Last target accepted'
 table=dict(id=next_id,title='Activity — current',type='table',datasource=ds,gridPos=dict(x=0,y=y,w=24,h=5),
    description=DESCRIPTION,targets=targets,
    transformations=[dict(id='joinByField',options=dict(byField='laser',mode='outerTabular')),
      dict(id='organize',options=dict(includeByName={'laser':True,**{'Value #'+r:True for r in columns}},
        indexByName={'laser':0,**{'Value #'+r:i+1 for i,r in enumerate(columns)}},
        renameByName={'laser':'Laser',**{'Value #'+r:label for r,label in columns.items()}}))],
    fieldConfig=dict(defaults=dict(unit='none',noValue='—',mappings=[]),overrides=[
      *[dict(matcher=dict(id='byName',options=label),properties=[dict(id='mappings',value=mappings()),
         dict(id='custom.cellOptions',value=dict(type='color-text'))]) for label in LANES.values()],
      dict(matcher=dict(id='byName',options='Last target accepted'),properties=[dict(id='unit',value='dateTimeAsLocal')])]),
    options=dict(showHeader=True,sortBy=[dict(displayName='Laser',desc=False)]))
 timeline=dict(id=next_id+1,title='Activity — timeline',type='state-timeline',datasource=ds,
    gridPos=dict(x=12,y=y+5,w=12,h=h),description=DESCRIPTION,
    targets=[dict(refId=chr(65+i),expr=query(lane,selector),legendFormat='{{laser}} · '+label,
        datasource=ds,instant=False,range=True) for i,(lane,label) in enumerate(LANES.items())],
    fieldConfig=dict(defaults=dict(unit='none',noValue='UNKNOWN',mappings=mappings(),
        color=dict(mode='palette-classic'),custom=dict(spanNulls=False,lineWidth=1)),overrides=[]),
    options=dict(mergeValues=True,showValue='auto',alignValue='left',rowHeight=.8,
        legend=dict(showLegend=False),tooltip=dict(mode='single')))
 return table,timeline


def apply(document):
 d=copy.deepcopy(document)
 if any(p.get('title')=='Activity — current' for p in d.get('panels',[])):
  for panel in d['panels']:
   if panel.get('type')!='state-timeline':continue
   if panel.get('title') in ('State','All lasers — state timeline'):
    panel['gridPos'].update(x=0,w=12)
   elif panel.get('title')=='Activity — timeline':
    panel['gridPos'].update(x=12,w=12)
  return d
 fleet=d.get('uid')=='ldcon-fleet';selector='laser!="",simulation="false"' if fleet else SELECTOR
 states=[p for p in d['panels'] if p['title'] in ('State and activity','Lock status','State','All lasers — current state','All lasers — state timeline')]
 timeline=next((p for p in states if p['type']=='state-timeline'),None)
 if not timeline:raise ValueError('No unique physical state timeline to split')
 for panel in states:
  for t in panel.get('targets',[]):
   if 'operation_state_code' in t.get('expr',''):t['expr']=state_only(t['expr'])
  if panel['title'] in ('State and activity','Lock status'):panel['title']='State'
  panel['description']='Physical lock, optical quality and owner state only. See Activity for monitoring, adjustment, target changes and sweeps. Freshness and fault precedence are unchanged.'
  if panel['type']!='table':panel['fieldConfig']['defaults']['mappings']=state_mappings()
  else:
   for o in panel['fieldConfig'].get('overrides',[]):
    if o.get('matcher',{}).get('options')=='State':
     for prop in o.get('properties',[]):
      if prop['id']=='mappings':prop['value']=state_mappings()
 anchor=timeline['gridPos']['y'];old_h=timeline['gridPos']['h'];height=max(8,old_h)
 for p in d['panels']:
  if p['gridPos']['y']>=anchor+old_h:p['gridPos']['y']+=height-old_h
  if p['gridPos']['y']>=anchor:p['gridPos']['y']+=5
 timeline['gridPos'].update(x=0,w=12,h=height)
 added=panels(timeline['datasource'],selector,fleet=fleet,next_id=max(p['id'] for p in d['panels'])+1,y=anchor,h=height)
 d['panels'].extend(added);d['panels'].sort(key=lambda p:(p['gridPos']['y'],p['gridPos']['x']))
 return d
