"""Time-aligned, dynamically configured quality overlays on existing panels."""
import copy
from ldcon.web.quality_threshold_metrics import FIELDS, GROUPS

DESCRIPTION = ('One quality limit per observable is shared by classification and maintenance review. '
    'Quality limit lines are the saved laser settings at each timestamp, '
    'collected read-only every 5 seconds. They are configured limits, not proof that an owner has adopted '
    'a pending change. Dashed: quality upper limit; dotted: last recorded quality baseline. '
    'Missing, ambiguous or stale metadata leaves a gap. Earlier history is not backfilled with today\'s settings. '
    'The baseline is a historical post-adjustment measurement, with its original source time; '
    'it is retained when a newer acquisition is pending or unavailable, even across context changes. '
    'It is a historical comparison, not a current-context validation, and never becomes fresh by rereading. '
    'Classification is an optical quality proxy, not proof of physical single-mode emission.')
SIDE_PEAK_COLORS = {'Height': '#5794F2', 'Prominence': '#FF9830'}
SIDE_PEAK_DESCRIPTION = DESCRIPTION.replace(
    'Dashed: quality upper limit; dotted: last recorded quality baseline.',
    'Blue: height; orange: prominence, including their limits. Solid: measurement; '
    'dashed: quality upper limit; dotted: last recorded quality baseline.')


def baseline_query(metric):
    if metric not in (*FIELDS, 'dither_response', 'source_end_unix_s'):
        raise ValueError('Unknown quality baseline metric')
    scope='laser=~"${laser:regex}",simulation="false",kind="dashboard_metadata",producer="dashboard-metadata"'
    source='ldcon_quality_baseline_reference_'+metric+'{'+scope+'}'
    health='ldcon_quality_baseline_reference_available{'+scope+'}'
    raw=('(last_over_time('+source+'[25s]) and '
         '(tlast_over_time('+source+'[25s]) == tlast_over_time('+health+'[25s])) and '
         '(last_over_time('+health+'[25s]) == 1))')
    return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1))'


def baseline_status_query():
    scope='laser=~"${laser:regex}",simulation="false",kind="dashboard_metadata",producer="dashboard-metadata"'
    state='ldcon_quality_baseline_acquisition_state_code{'+scope+'}'
    previous='ldcon_quality_baseline_reference_from_previous{'+scope+'}'
    raw=('((last_over_time('+state+'[25s]) + 10 * last_over_time('+previous+'[25s])) and '
         '(tlast_over_time('+state+'[25s]) == tlast_over_time('+previous+'[25s])))')
    return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1))'


def threshold_query(group, metric):
    if group not in GROUPS or metric not in FIELDS:
        raise ValueError('Unknown optical quality limit')
    scope='laser=~"${laser:regex}",simulation="false",kind="dashboard_metadata",producer="dashboard-metadata"'
    source='ldcon_quality_'+group+'_'+metric+'{'+scope+'}'
    health='ldcon_quality_thresholds_read_ok{'+scope+'}'
    raw=('(last_over_time('+source+'[25s]) and '
         '(tlast_over_time('+source+'[25s]) == tlast_over_time('+health+'[25s])) and '
         '(last_over_time('+health+'[25s]) == 1))')
    if group=='single_mode_max':
        raw='('+raw+' and (last_over_time(ldcon_quality_classification_enabled{'+scope+'}[25s]) == 1))'
    # Duplicate metadata producers are ambiguous, never choose the largest limit.
    return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1))'


def line_style(label, color, style):
    return dict(matcher=dict(id='byName',options=label), properties=[
        dict(id='color',value=dict(mode='fixed',fixedColor=color)),
        dict(id='custom.lineStyle',value=dict(fill=style)),
        dict(id='custom.lineInterpolation',value='stepAfter'),
        dict(id='custom.lineWidth',value=1),dict(id='custom.fillOpacity',value=0),
        dict(id='custom.showPoints',value='never'),dict(id='custom.spanNulls',value=False)])


def apply(dashboard):
    """Patch only existing quality panels, preserving IDs, layout and queries."""
    result=copy.deepcopy(dashboard)
    for panel in result.get('panels',[]):
        if panel.get('type')!='timeseries':
            continue
        title=panel.get('title','')
        if title.startswith('Position-free secondary peaks'):
            fields=[('Height','side_peak_height_max'),('Prominence','side_peak_prominence_max')]
        elif title.startswith('Main fringe peak width'):
            fields=[('Width','main_fwhm_fraction')]
        else:
            fields=[]
        if not fields and not title.startswith('Current dither response'):
            continue
        config=panel.setdefault('fieldConfig',{})
        overrides=config.setdefault('overrides',[])
        # Replace previously managed overlays, including legacy two-band lines.
        old_labels={t.get('legendFormat') for t in panel.get('targets',[])
                    if t.get('refId','').startswith(('quality_', 'baseline_'))}
        panel['targets']=[t for t in panel.get('targets',[]) if not t.get('refId','').startswith(('quality_', 'baseline_'))]
        overrides[:]=[o for o in overrides if o.get('matcher',{}).get('options') not in old_labels]
        if fields:
            existing={t['refId'] for t in panel.get('targets',[])}
            if len(fields)>1:
                for short,_ in fields:
                    label='Secondary peak '+short.lower()
                    matcher=dict(id='byName',options=label)
                    entry=next((o for o in overrides if o.get('matcher')==matcher),None)
                    if entry is None:
                        entry=dict(matcher=matcher,properties=[])
                        overrides.append(entry)
                    entry['properties']=[p for p in entry['properties'] if p['id'] not in ('color','custom.lineStyle')]
                    entry['properties'].extend([
                        dict(id='color',value=dict(mode='fixed',fixedColor=SIDE_PEAK_COLORS[short])),
                        dict(id='custom.lineStyle',value=dict(fill='solid'))])
            for short,metric in fields:
                for group,color,style,name in [('single_mode_max','orange','dash','quality limit')]:
                    color=SIDE_PEAK_COLORS.get(short,color)
                    ref='quality_'+group+'_'+metric
                    label=short+' '+name
                    if ref not in existing:
                        panel['targets'].append(dict(refId=ref,expr=threshold_query(group,metric),
                            legendFormat=label,datasource=panel['datasource'],instant=False,range=True))
                    overrides[:]=[o for o in overrides if o.get('matcher')!=dict(id='byName',options=label)]
                    overrides.append(line_style(label,color,style))
                label=short+' last-recorded baseline'
                panel['targets'].append(dict(refId='baseline_'+metric,expr=baseline_query(metric),
                    legendFormat=label,datasource=panel['datasource'],instant=False,range=True))
                overrides.append(line_style(label,SIDE_PEAK_COLORS.get(short,'purple'),'dot'))
            description=SIDE_PEAK_DESCRIPTION if len(fields)>1 else DESCRIPTION
            panel['description']=description
            config.setdefault('defaults',{}).setdefault('custom',{})['axisLabel']=(
                'Normalized peak metric (1)' if len(fields)>1 else 'Width / fringe period (1)')
        if title.startswith('Current dither response'):
            panel['targets']=[t for t in panel['targets'] if t.get('legendFormat') not in ('Boundary threshold','Last-recorded baseline')]
            overrides[:]=[o for o in overrides if o.get('matcher',{}).get('options') not in
                          ('Warning threshold','Boundary threshold','Quality limit','Last-recorded baseline')]
            for target in panel['targets']:
                if target.get('legendFormat') in ('Warning threshold','Quality limit'):
                    target['legendFormat']='Quality limit'
            overrides.append(line_style('Quality limit','orange','dash'))
            panel['targets'].append(dict(refId='baseline_dither_response',expr=baseline_query('dither_response')+' * 1000',
                legendFormat='Last-recorded baseline',datasource=panel['datasource'],instant=False,range=True))
            overrides.append(line_style('Last-recorded baseline','purple','dot'))
            panel['description']=DESCRIPTION+' The pinned search boundary remains a separate experiment guard.'
    quality_panels=[p for p in result.get('panels',[]) if p.get('title','').startswith(('Position-free secondary peaks','Main fringe peak width','Current dither response'))]
    if quality_panels:
        title='Last recorded quality baseline — acquisition time'
        panel=next((p for p in result['panels'] if p.get('title') in (title, 'Last accepted quality baseline')),None)
        if panel is None:
            panel=dict(id=max(p.get('id',0) for p in result['panels'])+1,type='stat',title=title,
                gridPos=dict(x=0,y=max(p.get('gridPos',{}).get('y',0)+p.get('gridPos',{}).get('h',0) for p in result['panels']),w=12,h=3))
            result['panels'].append(panel)
        panel.update(title=title,datasource=quality_panels[0]['datasource'],
            description='Original acquisition end time of the last recorded post-adjustment reference, including a retained previous reference. This is historical evidence, not live measurement freshness. See the separate acquisition status.',
            targets=[dict(refId='baseline_time',expr=baseline_query('source_end_unix_s')+' * 1000',
                          legendFormat='{{laser}}',datasource=quality_panels[0]['datasource'],instant=True,range=False)],
            fieldConfig=dict(defaults=dict(unit='dateTimeAsLocal',noValue='Not recorded'),overrides=[]),
            options=dict(reduceOptions=dict(calcs=['lastNotNull'],fields='',values=False),orientation='horizontal',textMode='value_and_name',colorMode='none',graphMode='none'))
        status_title='Latest quality baseline acquisition'
        status=next((p for p in result['panels'] if p.get('title')==status_title),None)
        if status is None:
            status=dict(id=max(p.get('id',0) for p in result['panels'])+1,type='stat',title=status_title,
                gridPos=dict(x=0,y=max(p.get('gridPos',{}).get('y',0)+p.get('gridPos',{}).get('h',0)
                                      for p in result['panels']),w=24,h=3))
            result['panels'].append(status)
        status.update(datasource=quality_panels[0]['datasource'],
            description='Latest acquisition state is independent of the historical reference lines. '
                'Unavailable includes a context change or expired acquisition window; no measurement is retried here.',
            targets=[dict(refId='baseline_acquisition',expr=baseline_status_query(),legendFormat='{{laser}}',
                          datasource=quality_panels[0]['datasource'],instant=True,range=False)],
            fieldConfig=dict(defaults=dict(noValue='Metadata unavailable',mappings=[dict(type='value',options={
                '0':dict(text='No record / unreadable',color='gray'),
                '1':dict(text='Pending — no recorded reference',color='orange'),
                '2':dict(text='Recorded',color='green'),
                '3':dict(text='Unavailable — no recorded reference',color='orange'),
                '11':dict(text='Pending — previous reference retained',color='orange'),
                '13':dict(text='Unavailable — previous reference retained',color='orange')})]),overrides=[]),
            options=dict(reduceOptions=dict(calcs=['lastNotNull'],fields='',values=False),orientation='horizontal',
                         textMode='value_and_name',colorMode='value',graphMode='none'))
    return result
