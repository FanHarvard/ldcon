"""DLC detail layout using shared measurement, quality and activity semantics."""
import copy

from ldcon.web.metric_query_profiles import fleet_measurement_queries
from ldcon.web.target_reference_panel import add_reference_panel


def complete_detail(document):
    result = add_reference_panel(document)
    measurements = fleet_measurement_queries('laser=~"${laser:regex}"')
    original = next(p for p in result['panels'] if p['title'] == 'Latest frequency offset (MHz)')
    summary = [
        ('Latest wavelength (nm)', 'wavelength', 9),
        ('Mean wavelength (5 min, nm)', 'wavelength_mean', 9),
        ('Mean offset (5 min, MHz)', 'offset_mean', 3),
        ('RMS offset (5 min, MHz)', 'rms', 3),
    ]
    for title, field, decimals in summary:
        panel = copy.deepcopy(original)
        panel.update(id=max(p['id'] for p in result['panels'])+1, title=title)
        panel['targets'][0].update(expr=measurements[field], legendFormat=title)
        panel['fieldConfig']['defaults'].update(decimals=decimals, unit='none')
        panel['description'] = ('Independent raw wavemeter samples; no lock-state filter. '
            'Latest expires after 25 s. Means use raw samples over 5 min. '
            'RMS includes bias relative to the fresh current configured target. '
            'Missing or ambiguous evidence remains blank. Numeric units are in the label.')
        result['panels'].append(panel)
    return align_detail(result)


def align_detail(document):
    """Match the common Sr422 layout, retaining real DLC measurement queries.

    Combine same-unit output series while keeping measured/bias/total settings
    explicitly named. No ECDL-only DAC, dither, hop or classification is added.
    This pure presentation migration is also usable on an existing dashboard.
    """
    result = copy.deepcopy(document)
    panels = result['panels']
    # These legacy diagnostics duplicate the quality overlays or never receive
    # DLC command_applied events. Do not accumulate obsolete panels below data.
    retired = {'Applied controller commands (events/min)',
               'Last recorded quality baseline — acquisition time',
               'Latest quality baseline acquisition'}
    panels[:] = [p for p in panels if p['title'] not in retired]
    from ldcon.web.metric_query_profiles import configured_target_query, SELECTOR
    from ldcon.web.dlcpro_target_query import effective_target_query
    configured = configured_target_query(SELECTOR)
    effective = effective_target_query()
    for panel in panels:
        changed = False
        for target in panel.get('targets', []):
            expr = target.get('expr', '')
            if effective not in expr and configured in expr:
                target['expr'] = expr.replace(configured, effective)
                changed = True
            if target.get('legendFormat') == 'Configured target wavelength (nm)':
                target['legendFormat'] = 'Effective target wavelength (nm)'
        if changed:
            panel['description'] = ('During control, use the newest owner-confirmed runtime target, '
                'including temporary study/scan targets; when idle use the saved target. '
                'Every historical sample uses the target at that time. Stale/ambiguous live '
                'evidence remains blank. Means and RMS use the displayed time window. '
                'The fixed-reference wavelength is display-only.')

    def find(title, kind):
        matches = [p for p in panels if p['title'] == title and p['type'] == kind]
        if len(matches) != 1:
            raise ValueError('Expected one '+kind+' panel: '+title)
        return matches[0]

    # Add a summary of an existing measured-temperature query, not a setting
    # relabeled as a measurement. All of its freshness guards are retained.
    if not any(p['title'] == 'Measured temperature (degC)' and p['type'] == 'stat' for p in panels):
        measured = find('Measured temperature (degC)', 'timeseries')
        temperature = copy.deepcopy(find('Measured diode current (mA)', 'stat'))
        temperature.update(id=max(p['id'] for p in panels)+1, title='Measured temperature (degC)',
                           description=measured['description'])
        temperature['targets'] = copy.deepcopy(measured['targets'])
        for target in temperature['targets']:
            target.update(instant=True, range=False)
        panels.append(temperature)

    def combine(title, titles, labels, axis):
        already = [p for p in panels if p['title'] == title and p['type'] == 'timeseries']
        if already:
            if len(already) != 1:
                raise ValueError('Ambiguous combined output panel')
            return already[0]
        sources = [find(name, 'timeseries') for name in titles]
        panel = sources[0]
        targets = []
        for source, label in zip(sources, labels):
            if len(source['targets']) != 1:
                raise ValueError('Output panel has unexpected multiple sources')
            target = copy.deepcopy(source['targets'][0])
            target.update(refId=chr(65+len(targets)), legendFormat=label)
            targets.append(target)
        panel.update(title=title, targets=targets)
        panel['description'] = ' '.join(source.get('description', '') for source in sources)
        panel['description'] += (' Blue solid: measured readback. Orange dashed: setpoint'
            ' (total current including internal feedforward in the current panel). '
            'Purple dotted: current bias setting before feedforward. Settings are not measured outputs. '
            'Missing or stale fields remain gaps; no value is synthesized.')
        panel['fieldConfig']['defaults']['custom']['axisLabel'] = axis
        panel['fieldConfig']['overrides'] = []
        styles = [('blue', 'solid'), ('orange', 'dash')] if len(labels) == 2 else [
            ('blue', 'solid'), ('purple', 'dot'), ('orange', 'dash')]
        for label, (color, style) in zip(labels, styles):
            panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options=label), properties=[
                dict(id='color', value=dict(mode='fixed', fixedColor=color)),
                dict(id='custom.lineStyle', value=dict(fill=style)),
                dict(id='custom.lineWidth', value=1)]))
        for source in sources[1:]:
            panels.remove(source)
        return panel

    current = combine('Diode current (mA)',
        ['Measured diode current (mA)', 'Current bias setpoint (mA)', 'Total current setpoint (mA)'],
        ['Measured current', 'Bias setpoint', 'Total setpoint (including FF)'], 'Current (mA)')
    temperature = combine('Temperature (degC)',
        ['Measured temperature (degC)', 'Temperature setpoint (degC)'],
        ['Measured temperature', 'Temperature setpoint'], 'Temperature (degC)')
    piezo = combine('PZT voltage (V)', ['Measured PZT voltage (V)', 'PZT voltage setpoint (V)'],
        ['Measured PZT voltage', 'PZT setpoint'], 'PZT voltage (V)')
    positioned = set()

    def position(panel, x, y, w, h):
        panel['gridPos'] = dict(x=x, y=y, w=w, h=h)
        positioned.add(panel['id'])

    for i, title in enumerate(['State', 'Latest frequency offset (MHz)', 'Measured diode current (mA)',
                               'Measured temperature (degC)', 'Measured PZT voltage (V)', 'Latest wavelength (nm)']):
        position(find(title, 'stat'), i*4, 0, 4, 4)
    latest = find('Latest frequency offset (MHz)', 'stat')
    latest['fieldConfig']['defaults']['decimals'] = 3
    latest['targets'][0]['legendFormat'] = 'Offset (MHz)'
    position(find('Activity — current', 'table'), 0, 4, 24, 5)
    state_timeline = find('State', 'state-timeline')
    position(state_timeline, 0, 9, 12, 8)
    state_timeline['options']['showValue'] = 'auto'
    position(find('Activity — timeline', 'state-timeline'), 12, 9, 12, 8)
    position(find('Target wavelength and fixed-reference offsets', 'timeseries'), 0, 17, 12, 8)
    offset = find('Frequency offset from target (MHz)', 'timeseries')
    position(offset, 12, 17, 12, 8)
    offset['fieldConfig']['defaults'].update(unit='none', decimals=3)
    offset['fieldConfig']['defaults']['custom']['axisLabel'] = 'Frequency offset (MHz)'
    series = [current, temperature, piezo, find('Position-free secondary peaks', 'timeseries'),
              find('Main fringe peak width', 'timeseries')]
    for i, panel in enumerate(series):
        position(panel, i*8 if i<3 else (i-3)*12, 25 if i<3 else 33, 8 if i<3 else 12, 8)
        panel['options']['legend'].update(displayMode='list', placement='bottom')
    for i, title in enumerate(['Mean wavelength (5 min, nm)', 'Mean offset (5 min, MHz)', 'RMS offset (5 min, MHz)']):
        position(find(title, 'stat'), i*8, 41, 8, 4)
    if positioned != {p['id'] for p in panels}:
        raise ValueError('Unknown panel would be lost during DLC layout migration')
    panels.sort(key=lambda p: (p['gridPos']['y'], p['gridPos']['x']))
    return result
