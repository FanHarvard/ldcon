"""Add a dual-axis target/reference history panel without changing laser settings."""
import copy
from ldcon.web.metric_query_profiles import configured_target_query, fleet_measurement_queries, SELECTOR


def add_reference_panel(document):
    result=copy.deepcopy(document)
    if any(p.get('title')=='Target wavelength and fixed-reference offsets' for p in result['panels']):
        raise ValueError('Reference panel already exists; inspect before adding another')
    reference=next((v for v in result['templating']['list'] if v['name']=='line_nm'),None)
    if reference is None:raise ValueError('An explicit fixed-reference wavelength is required')
    value=float(reference['current']['value'])
    if not 0<value<float('inf'):raise ValueError('Fixed reference must be positive and finite')
    reference.update(label='Fixed reference (nm)',hide=0,description='Display-only fixed wavelength for the new comparison panel. This does not change the laser target. The frequency-offset-from-target panel continues to use the laser configuration.')
    target=configured_target_query(SELECTOR)
    measured=fleet_measurement_queries('laser=~"${laser:regex}"')['wavelength']
    # Use the same measured wavelength source for conversion; unique source and
    # 25-second freshness rules remain enforced by the shared query profile.
    baseline='299792458e9 / ${line_nm}'
    expressions=[('Configured target wavelength (nm)','299792458e9 / ('+target+')'),
                 ('Target − fixed reference (MHz)','(('+target+') - ('+baseline+')) / 1e6'),
                 ('Measured − fixed reference (MHz)','((299792458e9 / ('+measured+')) - ('+baseline+')) / 1e6')]
    current=next(p for p in result['panels'] if p['type']=='timeseries' and p['title']=='Frequency offset from target (MHz)')
    panel=copy.deepcopy(current);panel.update(id=max(p['id'] for p in result['panels'])+1,title='Target wavelength and fixed-reference offsets')
    panel['description']='Left axis: saved laser target wavelength. Right axis: two separate frequency offsets (saved target and measured frequency), each minus the fixed reference entered in the dashboard Fixed reference (nm) field. This reference is display-only; editing it never changes a laser. The neighboring offset-from-target panel remains measured frequency minus the current saved target. Target history begins with the metadata collector; missing/stale target evidence leaves a gap. Measured data is independent of lock state. Units appear in axis labels, not tick values.'
    base=current['targets'][0]
    panel['targets']=[{**base,'refId':chr(65+i),'expr':expr,'legendFormat':name,'instant':False,'range':True} for i,(name,expr) in enumerate(expressions)]
    panel['fieldConfig']={'defaults':{'unit':'none','noValue':'—','decimals':9,'color':{'mode':'palette-classic'},
      'custom':{'axisPlacement':'left','axisLabel':'Target wavelength (nm)','lineWidth':1,'spanNulls':False,'showPoints':'never','drawStyle':'line'}},'overrides':[
       {'matcher':{'id':'byName','options':name},'properties':[{'id':'custom.axisPlacement','value':'right'},{'id':'custom.axisLabel','value':'Offset from fixed reference (MHz)'},{'id':'unit','value':'none'},{'id':'decimals','value':3}]}
       for name,_ in expressions[1:]]}
    panel['options']={'legend':{'displayMode':'list','placement':'bottom','calcs':['lastNotNull']},'tooltip':{'mode':'multi'}}
    y=current['gridPos']['y'];panel['gridPos']={'x':0,'y':y,'w':12,'h':8};current['gridPos']={'x':12,'y':y,'w':12,'h':8}
    others=[p for p in result['panels'] if p['type']=='timeseries' and p is not current]
    for i,p in enumerate(others):p['gridPos']={'x':i%4*6,'y':y+8+i//4*8,'w':6,'h':8}
    index=result['panels'].index(current);result['panels'].insert(index,panel)
    result['version']=result.get('version',0)+1
    return result
