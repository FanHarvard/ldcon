"""Offline Grafana export. All device coordinates come from the laser snapshot."""
import copy
import json
import math
import re
from pathlib import Path

C_NM=299792458e9
DEFAULTS=dict(reference_wavelength_nm=None,reference_frequency_hz=None,reference_card_id=None,
              datasource_uid='',title='',offset_range_mhz=1000000.,metric_overrides={},
              sample_interval_s=.5,max_sample_age_s=1.,report_card_ids=None,
              report_quantity_ids=None,display_tolerance_hz=None)
STATE_CODES={'stopped':0,'recovering':1,'acquiring':2,'locked':3,'hopped':4,
             'failed':5,'mode_degraded':6,'waiting_for_measurement':7,'tracking':8,
             'confirming_mode_loss':4,'mapping':8,'monitoring':8,'legacy_flag_false_pi_unknown':9,'native_pi_on_quality_unknown':10,'native_pi_held':11}
# DLC hold means mode=idle/PI disabled, not emission off. A programmed scan is
# physical tracking; its sweep lifecycle is shown separately in Activity.
STATE_CODES.update(hold=11, sweeping=8)


def parameters(card,overrides):
    if not isinstance(overrides,dict) or not isinstance(card['parameters'],dict):raise ValueError('Expected parameter mappings')
    if (set(card['parameters'])|set(overrides))-DEFAULTS.keys():raise ValueError('Unknown Grafana parameter')
    p=copy.deepcopy({**DEFAULTS,**card['parameters'],**overrides})
    for key in ('reference_wavelength_nm','reference_frequency_hz','offset_range_mhz','sample_interval_s','max_sample_age_s','display_tolerance_hz'):
        value=p[key]
        if value is None and (key.startswith('reference_') or key=='display_tolerance_hz'):continue
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('Invalid '+key)
    if p['reference_wavelength_nm'] is not None and p['reference_frequency_hz'] is not None:raise ValueError('Specify reference wavelength or frequency, not both')
    for key in ('datasource_uid','title'):
        if not isinstance(p[key],str) or len(p[key])>200:raise ValueError('Invalid '+key)
    if p['reference_card_id'] is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',str(p['reference_card_id'])):raise ValueError('Invalid reference card')
    from ldcon.core.card_reporting import REPORT_QUANTITIES
    from ldcon.core.metric_quantities import QUANTITIES
    for key in ('report_card_ids','report_quantity_ids'):
        value=p[key]
        if value is not None and (not isinstance(value,list) or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}',v) for v in value) or len(value)!=len(set(value))):raise ValueError('Invalid '+key)
    if not isinstance(p['metric_overrides'],dict) or any(k not in set(METRICS)|set(REPORT_QUANTITIES)|set(QUANTITIES) or not isinstance(v,str) or not v or len(v)>4096 for k,v in p['metric_overrides'].items()):raise ValueError('Invalid metric overrides')
    return p


def resolve_reference(p,deck,invocations,active=None):
    p=copy.deepcopy(p)
    if p['reference_wavelength_nm'] is not None:return p,'explicit wavelength'
    if p['reference_frequency_hz'] is not None:
        p['reference_wavelength_nm']=C_NM/p.pop('reference_frequency_hz');p['reference_frequency_hz']=None
        return p,'explicit frequency'
    requested=p['reference_card_id']
    candidates=[]
    if active:
        live=active.get('runtime_parameters',{})
        for c in active.get('cards',[]):
            if requested and c['id']!=requested:continue
            values=c.get('params',{});source='running card '+c['id']
            if (live.get('status')=='running' and live.get('job_id')==active.get('id')
                    and live.get('card_id')==c['id'] and live.get('revision')):
                values={**values,**live.get('parameters',{})}
                source+=' at runtime revision '+str(live['revision'])
            candidates.append((c['id'],values,source))
    cards=[c for c in deck['cards'] if c['operation']!='report.grafana_dashboard' and (not requested or c['id']==requested)]
    cards.sort(key=lambda c:0 if c['operation'].startswith('servo.') else 1)
    candidates.extend((c['id'],{**c['parameters'],**invocations.get(c['id'],{})},'assigned card '+c['id']) for c in cards)
    for _,values,source in candidates:
        frequency=values.get('target_frequency_hz');wavelength=values.get('target_wavelength_nm')
        if type(frequency) in (int,float) and math.isfinite(frequency) and frequency>0:p['reference_wavelength_nm']=C_NM/frequency;_reference_tolerance(p,values);return p,source
        if type(wavelength) in (int,float) and math.isfinite(wavelength) and wavelength>0:p['reference_wavelength_nm']=wavelength;_reference_tolerance(p,values);return p,source
    raise ValueError('Set a reference wavelength/frequency or select a card with a target')


def _reference_tolerance(p,values):
    tolerance=values.get('tolerance_hz')
    if p['display_tolerance_hz'] is None and type(tolerance) in (int,float) and math.isfinite(tolerance) and tolerance>0:
        p['display_tolerance_hz']=tolerance


from ldcon.web.metric_query_profiles import METRICS,report_queries
from ldcon.core.card_reporting import collect_reports
from ldcon.web.dashboard_state import state_mappings

# Identical priorities for every laser. Hardware-specific quantities are omitted
# when unavailable; they never change the ordering of common panels.
DISPLAY_ORDER = (
    'controller.state', 'optical.frequency', 'electrical.current',
    'model.current_estimate', 'control.current_setpoint', 'control.current_bias_setpoint',
    'control.current_total_setpoint', 'control.current_dac',
    'control.current_axis', 'control.temperature_axis', 'thermal.temperature',
    'control.temperature_setpoint', 'control.piezo_voltage', 'control.piezo_setpoint', 'control.piezo_dac',
    'optical.single_mode', 'optical.secondary_peaks', 'optical.main_peak_width',
    'response.current_dither', 'response.dither_quality', 'optical.mode_hops',
    'controller.actions',
)



def source_context(laser):
    context=dict(wsu_channel='',wsu_cycle='',pzt_host='',current_unit='',temperature_unit='',dither_vpp=None)
    for device in laser['devices'].values():
        c=device['connection'];source=c.get('wavemeter',c.get('source',c))
        if 'channel' in source and ('cycle' in source):
            context.update(wsu_channel=str(source['channel']),wsu_cycle=str(source['cycle']))
        pzt=c.get('resources',{}).get('piezo',{})
        if pzt.get('host'):context['pzt_host']=pzt['host']
        settings=device.get('control_settings',{});axes=settings.get('axes',{})
        for axis,key in [('current','current_unit'),('temperature','temperature_unit')]:
            if axes.get(axis,{}).get('unit'):context[key]=axes[axis]['unit']
        dither=settings.get('steady_lock',{}).get('dither',{})
        if type(dither.get('effective_amplitude_peak')) in (int,float):context['dither_vpp']=2*dither['effective_amplitude_peak']
    return context


def textbox(name,label,value):
    text=str(value)
    return dict(name=name,label=label,type='textbox',query=text,current=dict(text=text,value=text),options=[])


def _report_selection(laser, p, deck, card_parameters=None):
    if deck is None:
        raise ValueError('A deck snapshot is required to collect card reporting quantities')
    selection = collect_reports(deck, laser, p.get('report_card_ids'), p.get('report_quantity_ids'), card_parameters=card_parameters)
    context = source_context(laser)
    bindings = {}; unavailable = list(selection['unavailable_quantities'])
    for quantity in selection['selected_quantities']:
        if quantity['id'] == 'optical.wavelength':
            continue
        queries, reason = report_queries(quantity, laser, context, p['metric_overrides'],
                                         tolerance='${lock_tolerance_hz}' if p.get('display_tolerance_hz') else None)
        if reason:
            unavailable.append(dict(id=quantity['id'], reason=reason))
        else:
            bindings[quantity['id']] = queries
    if p.get('report_quantity_ids') is not None:
        rejected = set(p['report_quantity_ids']) - bindings.keys() - {'optical.wavelength'}
        if rejected:
            raise ValueError('No report source binding for selected quantities: '+', '.join(sorted(rejected)))
    selection['unavailable_quantities'] = unavailable
    selection['rendered_quantity_ids'] = list(bindings)
    return selection, bindings, context


def build_dashboard(laser,p,*,deck=None,card_parameters=None):
    if p['reference_wavelength_nm'] is None:
        raise ValueError('Resolve reference at execution time before export')
    selection, bindings, context = _report_selection(laser, p, deck, card_parameters)
    ds = dict(type='prometheus', uid='${DS_VM}')
    variables = [dict(name='DS_VM', label='Data source', type='datasource', query='prometheus', refresh=1,
                      current=dict(text=p['datasource_uid'], value=p['datasource_uid']), options=[]),
                 textbox('laser', 'Laser', laser['name']),
                 textbox('line_nm', 'Internal reference', format(p['reference_wavelength_nm'], '.17g')),
                 textbox('offset_range_mhz', 'Offset range (MHz)', p['offset_range_mhz'])]
    if p.get('display_tolerance_hz'):
        variables.append(textbox('lock_tolerance_hz', 'Configured lock tolerance (Hz)', p['display_tolerance_hz']))
    # Retain the exact internal conversion reference without a wavelength UI.
    next(v for v in variables if v['name'] == 'line_nm')['hide'] = 2
    for name in ('wsu_channel', 'wsu_cycle', 'pzt_host'):
        if context[name]:
            variables.append(textbox(name, name.replace('_', ' ').capitalize(), context[name]))
    stats = []; timelines = []; series = []

    def panel(quantity, queries, *, title=None, kind='timeseries', unit=None):
        title = title or quantity['title']
        unit = unit or quantity['unit']
        targets = []
        for i, (label, expr) in enumerate(queries):
            targets.append(dict(refId=chr(65+i), expr=expr, legendFormat=label, datasource=ds,
                                instant=kind=='stat', range=kind!='stat'))
        defaults = dict(unit='none', noValue='Unknown', mappings=[], color=dict(mode='palette-classic'),
                        thresholds=dict(mode='absolute', steps=[dict(color='green', value=None)]))
        if kind=='timeseries':
            defaults['custom'] = dict(lineWidth=1, spanNulls=False, showPoints='never', axisLabel=title, drawStyle='line')
        options = dict(legend=dict(displayMode='list', placement='bottom'), tooltip=dict(mode='multi'))
        if kind=='stat':
            options = dict(reduceOptions=dict(calcs=['lastNotNull'], fields='', values=False), colorMode='none', graphMode='none', textMode='value')
        if quantity['id']=='controller.state':
            defaults['mappings'] = state_mappings()
            if kind=='stat': options['colorMode']='background'
            if kind=='state-timeline':
                options.update(showValue='always', alignValue='left', mergeValues=True, rowHeight=.8)
                defaults['custom'] = dict(spanNulls=False, lineWidth=1)
        value = dict(title=title, type=kind, datasource=ds, targets=targets,
                     description=quantity['meaning']+' '+quantity['constraints']+' Reporting cards: '+', '.join(quantity['card_ids'])+'.',
                     fieldConfig=dict(defaults=defaults, overrides=[]), options=options)
        (stats if kind=='stat' else timelines if kind=='state-timeline' else series).append(value)
        return value

    for quantity in sorted(selection['selected_quantities'], key=lambda q: DISPLAY_ORDER.index(q['id']) if q['id'] in DISPLAY_ORDER else len(DISPLAY_ORDER)):
        identifier = quantity['id']
        if identifier not in bindings:
            continue
        queries = bindings[identifier]
        if identifier=='controller.state':
            panel(quantity, queries, kind='stat', title='Lock status')
            panel(quantity, queries, kind='state-timeline', title='Lock status')
            continue
        if identifier=='optical.frequency':
            from ldcon.web.metric_query_profiles import fleet_measurement_queries
            expr = fleet_measurement_queries('laser=~"${laser:regex}"')['offset']
            panel(quantity, [('Offset', expr)], kind='stat', title='Latest frequency offset (MHz)', unit='MHz')
            value = panel(quantity, [('Offset (MHz)', expr)], title='Frequency offset from target (MHz)', unit='MHz')
            value.update(interval='10ms', maxDataPoints=8192)
            value['options']['legend'].update(showLegend=True,displayMode='list',calcs=['mean','stdDev'])
            value['description'] += ' Uses the fresh saved laser target at each timestamp. Missing target metadata leaves a gap; no fixed reference or lock-state filter.'
            continue
        if identifier=='optical.mode_hops':
            panel(quantity, [('Confirmed hops', 'sum(sum_over_time('+queries[0][1]+'[$__range]))')], kind='stat', title='Hops in range')
            panel(quantity, [('Confirmed hops / min', 'sum by(laser) (sum_over_time('+queries[0][1]+'[1m]))')], unit='events/min')
            continue
        display_unit = quantity['unit']
        if display_unit == 'A':
            queries = [(label, '('+expr+') * 1000') for label, expr in queries]
            display_unit = 'mA'
        if identifier=='response.current_dither' and display_unit=='nm/V':
            queries = [(label, '('+expr+') * 1000') for label, expr in queries]
            display_unit = 'fm/mV'
        value = panel(quantity, queries, title=quantity['title']+' ('+display_unit+')' if display_unit not in ('1','mixed') else quantity['title'], unit=display_unit)
        if identifier=='response.current_dither':
            value['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=label), properties=[dict(id='color', value=dict(mode='fixed', fixedColor=color))])
                for label, color in [('Response','green'), ('Warning threshold','orange'), ('Boundary threshold','red')]]
        if identifier=='response.dither_quality':
            value['fieldConfig']['defaults']['custom']['axisLabel'] = 'Valid fit (1=yes)'
            value['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options='Source age (s)'),
                properties=[dict(id='unit', value='none'), dict(id='custom.axisPlacement', value='right'), dict(id='custom.axisLabel', value='Source age (s)')])]
        if identifier == 'controller.actions':
            value['fieldConfig']['defaults']['noValue'] = 'No recorded commands'
            value['description'] += ' Native PI updates are not command_applied events. An absent event series is not a measured zero or a stopped PI.'
        if identifier in ('electrical.current','control.current_axis','control.temperature_axis','control.piezo_voltage','control.current_dac','optical.fringe_fill'):
            panel(quantity, [(label, 'last_over_time(('+expr+')[30s:1s])') for label, expr in queries], kind='stat', title=value['title'], unit=display_unit)
    # Summary count follows selected quantities; detailed plots always retain
    # four columns. No phantom placeholders are added for absent ECDL hardware.
    for i, value in enumerate(stats):
        row = i//6; count = min(6, len(stats)-row*6); column=i%6
        value['gridPos'] = dict(x=column*24//count, y=row*4, w=(column+1)*24//count-column*24//count, h=4)
    y = ((len(stats)+5)//6)*4
    for value in timelines:
        value['gridPos'] = dict(x=0,y=y,w=24,h=4);y+=4
    for i, value in enumerate(series):
        value['gridPos'] = dict(x=(i%4)*6,y=y+(i//4)*8,w=6,h=8)
    panels = stats+timelines+series
    for i, value in enumerate(panels):value['id']=i+1
    dashboard = dict(id=None,uid='ldcon-'+laser['id'],title=p['title'] or 'Laser control: '+laser['name'],schemaVersion=42,version=1,
        editable=True,tags=['ldcon','laser-control','laser-detail'],timezone='browser',refresh='1s',time=dict(**{'from':'now-5m','to':'now'}),
        templating=dict(list=variables),panels=panels,annotations=dict(list=[]),
        links=[dict(title='All lasers', type='link', url='/d/ldcon-fleet', targetBlank=False)])
    from ldcon.web.grafana_quality import apply
    from ldcon.web.grafana_activity import apply as activity
    dashboard = activity(apply(dashboard))
    if any(d.get('type') == 'dlcpro.laser.owner' for d in laser.get('devices', {}).values()):
        from ldcon.web.grafana_dlcpro import complete_detail
        dashboard = complete_detail(dashboard)
    return dashboard


def build_fleet_dashboard(datasource_uid=''):
    """Offline, automatically discovered fleet overview; no report card needed."""
    from ldcon.web.metric_query_profiles import fleet_status_query, fleet_measurement_queries
    ds = dict(type='prometheus', uid='${DS_VM}')
    query = fleet_status_query()
    defaults = dict(unit='none', noValue='UNKNOWN', mappings=state_mappings(),
                    color=dict(mode='thresholds'), thresholds=dict(mode='absolute', steps=[dict(color='gray', value=None)]))
    quantities=fleet_measurement_queries()
    columns={'A':'State','B':'Latest wavelength (nm)','C':'Latest offset (MHz)',
             'D':'Latest sample age (s)','E':'Mean wavelength (5 min, nm)',
             'F':'Mean offset (5 min, MHz)','G':'RMS offset (5 min, MHz)','H':'Configured target (nm)'}
    table = dict(id=1, title='All lasers — current state', type='table', datasource=ds,
        gridPos=dict(x=0, y=0, w=24, h=6),
        targets=[dict(refId=ref,expr=expr,datasource=ds,instant=True,range=False,format='table')
                 for ref,expr in zip(columns,[query,quantities['wavelength'],quantities['offset'],quantities['sample_age'],
                    quantities['wavelength_mean'],quantities['offset_mean'],quantities['rms'],quantities['reference']])],
        transformations=[dict(id='joinByField',options=dict(byField='laser',mode='outerTabular')),
            dict(id='organize',options=dict(
                includeByName={'laser':True,**{'Value #'+k:True for k in columns}},
                indexByName={'laser':0,**{'Value #'+k:i+1 for i,k in enumerate(columns)}},
                renameByName={'laser':'Laser',**{'Value #'+k:v for k,v in columns.items()}}))],
        fieldConfig=dict(defaults=dict(unit='none',noValue='—',mappings=[],color=dict(mode='fixed',fixedColor='text')),
            overrides=[dict(matcher=dict(id='byName',options='State'),properties=[
                dict(id='mappings',value=state_mappings()),
                dict(id='custom.cellOptions',value=dict(type='color-text'))]),
                *[dict(matcher=dict(id='byName',options=columns[k]),properties=[dict(id='decimals',value=9)]) for k in ('B','E','H')],
                *[dict(matcher=dict(id='byName',options=columns[k]),properties=[dict(id='decimals',value=3)]) for k in ('C','D','F','G')]]),
        options=dict(showHeader=True,sortBy=[dict(displayName='Laser',desc=False)]))
    timeline = dict(id=2, title='All lasers — state timeline', type='state-timeline', datasource=ds,
        gridPos=dict(x=0, y=6, w=24, h=8),
        targets=[dict(refId='A', expr=query, legendFormat='{{laser}}', datasource=ds, instant=False, range=True)],
        fieldConfig=dict(defaults={**copy.deepcopy(defaults), 'color':dict(mode='palette-classic'), 'custom': dict(spanNulls=False, lineWidth=1)}, overrides=[]),
        options=dict(mergeValues=True, showValue='auto', alignValue='left', rowHeight=.8,
                     legend=dict(displayMode='list', placement='bottom'), tooltip=dict(mode='single')))
    description = ('Lasers are discovered from real LDCon telemetry, retained as UNKNOWN for 30 days after their last record. '
                   'Only an explicit fresh output-disabled readback means OFF. HELD means PI is disabled, not emission off. '
                   'Active sweep, target, operating-point and study cards are shown as operations; fault, read failure and output-off take precedence. Monitor/agent events do not replace servo state. Missing or ambiguous physical evidence is UNKNOWN. '
                   'Display expiry is 25 seconds; this is not a control tolerance. Per-laser detail dashboards remain separate.')
    for panel in (table, timeline):
        panel['description'] = description
    table['description'] += (' Measurements do not depend on LOCKED: use raw wavemeter samples from a unique real cycle '
        '(exclude aggregate cycle -1). Latest is the last sample within 25 seconds; Mean is the raw-sample arithmetic mean '
        'over 5 minutes. Offset = measured frequency minus the current saved laser target, read independently every 5 seconds. '
        'Target metadata expires after 25 seconds. RMS = sqrt(frequency variance + mean offset squared), relative to '
        'the current saved target (not historical targets within the averaging window) '
        'over 5 minutes, including bias. No resampling or zero-filled gaps. Missing, stale or ambiguous measurements remain '
        'blank; missing reference leaves only offset/RMS blank. All numeric units are in column labels.')
    document = dict(id=None, uid='ldcon-fleet', title='LDCon — all lasers', schemaVersion=42, version=1,
        editable=True, tags=['ldcon', 'laser-control', 'fleet'], timezone='browser', refresh='5s',
        time={'from': 'now-1h', 'to': 'now'}, panels=[table, timeline], annotations=dict(list=[]),
        templating=dict(list=[dict(name='DS_VM', label='Data source', type='datasource', query='prometheus',
            refresh=1, current=dict(text=datasource_uid, value=datasource_uid), options=[])]),
        links=[dict(title='Laser detail dashboards', type='dashboards', tags=['laser-detail'],
                    asDropdown=True, includeVars=False, keepTime=True, targetBlank=False)])
    from ldcon.web.grafana_activity import apply
    return apply(document)


def normalize_dashboard(document):
    """Apply presentation fixes to an actual exported detail dashboard.

    Preserve UID, panel IDs, source/reference values, annotations, visual
    thresholds and device-specific panels. Unknown panel quantities fail closed
    instead of guessing a physical unit. No import/publication is performed.
    """
    from ldcon.web.metric_query_profiles import SELECTOR, fleet_status_query, owner_metric
    dashboard = copy.deepcopy(document)
    aliases = {
        'Lock status': ('controller.state', 'Lock status', None),
        'Frequency offset': ('optical.frequency', 'Frequency offset', 'MHz'),
        'Frequency offset from reference': ('optical.frequency', 'Frequency offset from reference', 'MHz'),
        'Measured diode current': ('electrical.current', 'Measured diode current', 'mA'),
        'Estimated modulated current': ('model.current_estimate', 'Estimated modulated current', 'mA'),
        'Current setpoint': ('control.current_setpoint', 'Current setpoint', 'mA'),
        'Current modulation DAC': ('control.current_dac', 'Current modulation DAC', 'count'),
        'Laser current input': ('control.current_axis', 'Current-controller input', 'V'),
        'Current-controller input': ('control.current_axis', 'Current-controller input', 'V'),
        'TEC input': ('control.temperature_axis', 'TEC-controller input', 'V'),
        'TEC-controller input': ('control.temperature_axis', 'TEC-controller input', 'V'),
        'Measured temperature': ('thermal.temperature', 'Measured temperature', 'K'),
        'Temperature setpoint': ('control.temperature_setpoint', 'Temperature setpoint', 'degC'),
        'Piezo input': ('control.piezo_voltage', 'Measured PZT voltage', 'V'),
        'Measured PZT voltage': ('control.piezo_voltage', 'Measured PZT voltage', 'V'),
        'Lock DAC': ('control.piezo_dac', 'PZT control DAC', 'count'),
        'PZT control DAC': ('control.piezo_dac', 'PZT control DAC', 'count'),
        'Fringe fill factor F2': ('optical.main_peak_width', 'Fringe fill factor F2 — recorded proxy', None),
        'Single-mode measure': ('optical.main_peak_width', 'Fringe fill factor F2 — recorded proxy', None),
        'Qualified single-mode classification': ('optical.single_mode', 'Qualified single-mode classification', None),
        'Main fringe peak width': ('optical.main_peak_width', 'Main fringe peak width', None),
        'Position-free secondary peaks': ('optical.secondary_peaks', 'Position-free secondary peaks', None),
        'Dither response': ('response.current_dither', 'Current dither response', 'fm/mV'),
        'Current dither response': ('response.current_dither', 'Current dither response', 'fm/mV'),
        'Dither analysis freshness': ('response.dither_quality', 'Dither analysis freshness', None),
        'Hops in range': ('optical.mode_hops', 'Hops in range', None),
        'Confirmed mode hops': ('optical.mode_hops', 'Confirmed mode hops', 'events/min'),
        'Controller actions': ('controller.actions', 'Applied controller commands', 'events/min'),
        'Applied controller commands': ('controller.actions', 'Applied controller commands', 'events/min'),
    }
    groups = {'stat': [], 'state-timeline': [], 'timeseries': []}
    changes = []
    coordinate_units = {}
    for source_panel in dashboard.get('panels', []):
        matched = re.fullmatch(r'(.+) \((V|A|mA|K|degC|count)\)', source_panel['title'])
        if matched:
            coordinate_units.setdefault(matched[1], set()).add(matched[2])
    for panel in dashboard.get('panels', []):
        old_title = panel['title']
        if 'wavelength' in old_title.lower():
            changes.append(dict(panel_id=panel['id'], action='remove wavelength display'))
            continue
        if old_title in ('Fringe fill factor F2', 'Single-mode measure (fringe fill factor)'):
            # Replace obsolete F2 displays with the current classification and
            # qualified peak-width measurement. No F2 thresholds are reused.
            classification = panel['type'] == 'stat'
            panel['title'] = ('Qualified single-mode classification' if classification else 'Main fringe peak width')
            field = 'single_mode' if classification else 'fringe_metrics.main_fwhm_fraction'
            first = panel['targets'][0]
            panel['targets'] = [{**first, 'expr': owner_metric(field), 'legendFormat': panel['title'],
                                 'instant': classification, 'range': not classification}]
            panel['fieldConfig'] = dict(defaults=dict(unit='none', mappings=[], color=dict(mode='palette-classic'),
                thresholds=dict(mode='absolute', steps=[dict(color='gray', value=None)])), overrides=[])
            if classification:
                panel['fieldConfig']['defaults']['mappings'] = [dict(type='value', options={
                    '0': dict(text='DEGRADED', color='orange'), '1': dict(text='QUALIFIED', color='green')})]
            panel['description'] = ('Classification under the configured optical calibration; no F2 thresholds or universal purity claims.'
                                    if classification else 'Main peak FWHM normalized by the qualified fringe period; not laser linewidth. No F2 thresholds are used.')
            changes.append(dict(panel_id=panel['id'], old_title=old_title, action='replace obsolete F2 display with current qualified optical quantity'))
            old_title = panel['title']
        if panel['type'] not in groups:
            raise ValueError('Review unsupported panel before normalization: ' + old_title)
        title = re.sub(r'\s*\([^)]*\)\s*$', '', old_title)
        if title not in aliases:
            raise ValueError('Review unknown quantity before normalization: ' + old_title)
        quantity, title, unit = aliases[title]
        if quantity in ('control.current_axis', 'control.temperature_axis'):
            source_name = re.sub(r'\s*\([^)]*\)\s*$', '', old_title)
            proven = coordinate_units.get(source_name, set())
            if len(proven) != 1:
                raise ValueError('Review missing/ambiguous actuator coordinate unit: ' + old_title)
            unit = next(iter(proven))
        defaults = panel.setdefault('fieldConfig', {}).setdefault('defaults', {})
        prior_unit = defaults.get('unit')
        if unit == 'mA' and (prior_unit == 'amp' or old_title.endswith('(A)')):
            for target in panel.get('targets', []):
                target['expr'] = '(' + target['expr'] + ') * 1000'
            for step in defaults.get('thresholds', {}).get('steps', []):
                if step.get('value') is not None:
                    step['value'] *= 1000
            for field in ('min', 'max'):
                if field in defaults:
                    defaults[field] *= 1000
            changes.append(dict(panel_id=panel['id'], action='scale amperes to milliamperes; physical values unchanged'))
        elif unit == 'mA' and prior_unit not in ('mamp', 'none') and not old_title.endswith('(mA)'):
            raise ValueError('Current scaling is ambiguous: ' + old_title)
        panel['title'] = title + (' (' + unit + ')' if unit else '')
        defaults['unit'] = 'none'
        defaults['noValue'] = 'Unknown'
        if panel['type'] == 'timeseries':
            defaults.setdefault('custom', {}).update(axisLabel=panel['title'], lineWidth=1, spanNulls=False)
        for override in panel['fieldConfig'].get('overrides', []):
            for prop in override.get('properties', []):
                if prop['id'] == 'unit':
                    if prop['value'] not in ('s', 'none', 'suffix:count'):
                        raise ValueError('Review override axis units: ' + old_title)
                    prop['value'] = 'none'
        # Older saved queries used dotted field paths as bare metric names.
        for target in panel.get('targets', []):
            target['expr'] = re.sub(r'\b(ldcon_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)\{',
                                    lambda m: '{__name__=' + json.dumps(m[1]) + ',', target.get('expr', ''))
        if quantity == 'controller.state':
            first = panel['targets'][0]
            panel['targets'] = [{**first, 'expr': fleet_status_query(SELECTOR), 'legendFormat': 'State',
                                 'instant': panel['type'] == 'stat', 'range': panel['type'] != 'stat'}]
            defaults['mappings'] = state_mappings()
            defaults['custom'] = dict(spanNulls=False, lineWidth=1)
            if panel['type'] == 'state-timeline':
                defaults['color'] = dict(mode='palette-classic')
                panel.setdefault('options', {}).update(mergeValues=True, showValue='auto')
        if quantity == 'response.dither_quality':
            defaults.setdefault('custom', {})['axisLabel'] = 'Valid fit (1=yes)'
            panel['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options='Source age (s)'),
                properties=[dict(id='unit', value='none'), dict(id='custom.axisPlacement', value='right'),
                            dict(id='custom.axisLabel', value='Source age (s)')])]
        if quantity == 'control.piezo_voltage':
            from ldcon.web.metric_query_profiles import pzt_driver_query
            for target in panel['targets']:
                field = ('output_voltage' if 'laser_servo_driver_output_voltage' in target['expr']
                         else 'dac_val' if 'laser_servo_driver_dac_val' in target['expr'] else None)
                if field:target['expr']=pzt_driver_query(field)
            panel['description']='Latest measured driver sample per physical host at each time. Experiment shot_file labels do not create separate curves. Voltage is measured, not inferred from DAC.'
        if quantity == 'controller.actions':
            for target in panel['targets']:
                if 'ldcon_applied' in target['expr'] or 'ldcon_action_event' in target['expr']:
                    target['expr'] = '(sum by(laser) (count_over_time(ldcon_action_event{' + SELECTOR + ',kind="command_applied"}[1m]))) or on(laser) (sum by(laser) (count_over_time(ldcon_applied{' + SELECTOR + ',kind="command_applied"}[1m])))'
            defaults['noValue'] = 'No recorded commands'
            panel['description'] = 'Recorded command_applied events. Native PI updates are not these events. No recorded commands does not mean zero activity or PI stopped.'
        if quantity == 'optical.mode_hops':
            defaults['noValue'] = 'No recorded hops'
            panel['description'] = 'Confirmed recorded hop events in this interval. An absent event stream is not a measured zero or proof that no hop occurred.'
        if 'Fringe fill factor' in panel['title']:
            panel['description'] = 'Recorded fringe proxy only. F2 alone does not establish single-mode operation. Existing quality criteria are unchanged.'
        if quantity == 'model.current_estimate':
            panel['description'] = 'Bias setpoint plus nominal modulation transfer, with calibration uncertainty. This estimate is separate from measured diode current.'
        groups[panel['type']].append((DISPLAY_ORDER.index(quantity), panel))
        changes.append(dict(panel_id=panel['id'], old_title=old_title, title=panel['title'], action='shared ordering and numeric axis formatting'))
    stats, timelines, series = ([p for _, p in sorted(groups[k], key=lambda pair: pair[0])]
                                 for k in ('stat', 'state-timeline', 'timeseries'))
    for i, panel in enumerate(stats):
        row, col = divmod(i, 6); count = min(6, len(stats) - row * 6)
        panel['gridPos'] = dict(x=col * 24 // count, y=row * 4, w=(col + 1) * 24 // count - col * 24 // count, h=4)
    y = ((len(stats) + 5) // 6) * 4
    for panel in timelines:
        panel['gridPos'] = dict(x=0, y=y, w=24, h=4); y += 4
    for i, panel in enumerate(series):
        panel['gridPos'] = dict(x=i % 4 * 6, y=y + i // 4 * 8, w=6, h=8)
    dashboard['panels'] = stats + timelines + series
    if 'laser-detail' not in dashboard.setdefault('tags', []):
        dashboard['tags'].append('laser-detail')
    for variable in dashboard.get('templating', {}).get('list', []):
        if variable['name'] == 'line_nm':
            variable['hide'] = 2
    if not any(link.get('url') == '/d/ldcon-fleet' for link in dashboard.get('links', [])):
        dashboard.setdefault('links', []).append(dict(title='All lasers', type='link', url='/d/ldcon-fleet', targetBlank=False))
    return dashboard, changes


def export_dashboard(directory,laser,p,*,reference_source='invocation',deck=None,card_parameters=None):
    selection, bindings, _ = _report_selection(laser, p, deck, card_parameters)
    dashboard=build_dashboard(laser,p,deck=deck,card_parameters=card_parameters)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    path=directory/'grafana-dashboard.json';path.write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n')
    manifest=dict(reference_wavelength_nm=p['reference_wavelength_nm'],reference_source=reference_source,
        display_tolerance_hz=p.get('display_tolerance_hz'),laser=laser['name'],device_io=False,panels=len(dashboard['panels']),
        report_selection=selection,metric_bindings={q:[dict(label=label,query=query) for label,query in queries] for q,queries in bindings.items()},
        notes=['Quantities are declared by the selected deck cards; query/source bindings are a separate profile.',
               'Native current DAC/current estimates are distinct from measured current and ECDL PZT coordinates.',
               'Fringe and dither quantities are qualified pattern-dependent proxies, not universal mode-purity measurements.',
               'A missing series stays absent; export does not establish that a measurement exists or is fresh.',
               'No Grafana import, publication or device I/O is performed.'])
    (directory/'grafana-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return dict(status='succeeded',artifacts=['grafana-dashboard.json','grafana-manifest.json'],output_writes=0,**manifest)
