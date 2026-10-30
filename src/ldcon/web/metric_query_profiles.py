"""LDCon Grafana storage/query profiles; contains no quantity definitions.

Preserve existing dashboards while storage backends and contracts evolve.
These display expressions may aggregate/hold values; they are not control samples.
"""
SELECTOR='laser=~"${laser:regex}",simulation="false"'
TRACE=SELECTOR+',kind="trace"'
def pzt_driver_query(field):
    """Latest physical host sample, independent of experiment shot_file tags."""
    if field not in ('output_voltage','dac_val'):raise ValueError('Unsupported driver field')
    raw='laser_servo_driver_'+field+'{host="${pzt_host}"}'
    stamp='tlast_over_time('+raw+'[30s])'
    return ('max by(host) (last_over_time('+raw+'[30s]) and ('+stamp+
            ' == on(host) group_left max by(host) ('+stamp+')))')


METRICS={
 'frequency':'299792458e9 / hf_wsu_wavelength_nm{laser=~"${laser:regex}",channel="${wsu_channel}",cycle="${wsu_cycle}"}',
 'lock_status':'ldcon_controller_state_code{'+SELECTOR+',kind="controller_status"}',
 'locked':'ldcon_locked{'+TRACE+'}',
 'hop':'ldcon_mode_hop{'+SELECTOR+',kind="ecdl_response_boundary"}',
 'dither':'(ldcon_dither_response{'+SELECTOR+',kind="owner_sample"} and on(laser,card) (ldcon_dither_response_valid{'+SELECTOR+',kind="owner_sample"} == 1))',
 'dither_valid':'ldcon_dither_response_valid{'+SELECTOR+',kind="owner_sample"}',
 'dither_age':'ldcon_dither_source_age_s{'+SELECTOR+',kind="owner_sample"}',
 'dither_warning':'ldcon_dither_warning_threshold{'+SELECTOR+',kind="owner_sample"}',
 'dither_edge':'ldcon_dither_edge_threshold{'+SELECTOR+',kind="owner_sample"}',
 'action':'sum by(laser) (count_over_time(ldcon_applied{'+SELECTOR+',kind="command_applied"}[1m]))',
 'current':'{__name__="ldcon_outputs.current",'+TRACE+'}', 'temperature':'{__name__="ldcon_outputs.temperature",'+TRACE+'}',
 'piezo':'{__name__="ldcon_outputs.piezo",'+TRACE+'}',
 'piezo_voltage':pzt_driver_query('output_voltage'),
 'piezo_dac':'laser_servo_driver_dac_val{host="${pzt_host}"}',
}

# Continuous owner readings take precedence over card traces. Historical traces
# remain visible before monitoring was enabled. Job selection filters card data;
# physical readbacks remain available independently of the selected card.
for _name, _field in dict(current='outputs.current', temperature='outputs.temperature',
                          piezo='outputs.piezo').items():
    _monitor='{__name__="ldcon_'+_field+'",laser=~"${laser:regex}",simulation="false",kind="owner_sample"}'
    # Short validity prevents Prometheus lookback from holding a failed reading.
    _monitor='last_over_time('+_monitor+'[25s])'
    METRICS[_name]='('+_monitor+') or ('+METRICS[_name]+' unless on(laser) ('+_monitor+'))'


def owner_metric(name):
    # Telegraf preserves nested dotted fields. They require an __name__ matcher,
    # rather than an invalid bare PromQL identifier containing a dot.
    metric = ('{__name__="ldcon_'+name+'",' if '.' in name else 'ldcon_'+name+'{')
    source = metric + SELECTOR + ',kind="owner_sample"}'
    raw = 'last_over_time(' + source + '[25s])'
    if name != 'read_ok':
        health = 'ldcon_read_ok{' + SELECTOR + ',kind="owner_sample"}'
        raw = '(' + raw + ' and (tlast_over_time(' + source + '[25s]) == tlast_over_time(' + health + '[25s])) and (last_over_time(' + health + '[25s]) == 1))'
    return 'max by(laser) (' + raw + ')'

for _name,_field in dict(dither_valid='dither_response_valid',dither_age='dither_source_age_s',dither_warning='dither_warning_threshold',dither_edge='dither_edge_threshold').items():
    METRICS[_name]=owner_metric(_field)
METRICS['dither']='('+owner_metric('dither_response')+') and on(laser) ('+METRICS['dither_valid']+' == 1)'


def native_status_query(reference='299792458e9 / ${line_nm}', tolerance=None):
    native=owner_metric('native_pi_enabled');valid=owner_metric('frequency_valid');single=owner_metric('single_mode');frequency=owner_metric('frequency_hz')
    enabled='('+native+' == 1)';fresh='('+valid+' == 1)'
    # Without a resolved device/card tolerance, PI enable is not proof of lock.
    unknown='(10 + 0 * '+enabled+')';held='(11 + 0 * ('+native+' == 0))'
    if tolerance is None:
        return unknown+' or on(laser) '+held
    near='(abs(('+frequency+') - ('+reference+')) <= '+str(tolerance)+')'
    base='('+enabled+' and on(laser) '+fresh+' and on(laser) '+near+')'
    clean='(3 + 0 * ('+base+' and on(laser) ('+single+' == 1)))'
    degraded='(6 + 0 * ('+base+' and on(laser) ('+single+' == 0)))'
    tracking='(8 + 0 * ('+enabled+' and on(laser) '+fresh+' and on(laser) (abs(('+frequency+') - ('+reference+')) > '+str(tolerance)+')))'
    return ' or on(laser) '.join([clean,degraded,tracking,unknown,held])


def configured_target_query(selector='laser!="",simulation="false"'):
    """Fresh configured target, never an inferred or last-used lock target."""
    scope=selector+',kind="dashboard_metadata",producer="dashboard-metadata"'
    target='ldcon_configured_target_frequency_hz{'+scope+'}'
    valid='ldcon_target_valid{'+scope+'}'
    raw='(last_over_time('+target+'[25s]) and (tlast_over_time('+target+'[25s]) == tlast_over_time('+valid+'[25s])) and (last_over_time('+valid+'[25s]) == 1))'
    return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1))'


def fleet_inventory_query(selector='laser!="",simulation="false"'):
    """Retain offline identities, but only their latest registered display name.

    A rename/case correction must not leave a second UNKNOWN laser for 30 days.
    Match by service_id + laser_id before projecting the Grafana display label.
    Conflicting names at the same latest instant are excluded as ambiguous.
    """
    metric='ldcon_inventory_present{'+selector+',producer="web-fleet-inventory",kind="inventory",service_id!="",laser_id!=""}'
    times='tlast_over_time('+metric+'[30d])'
    latest='('+times+' == on(service_id,laser_id) group_left max by(service_id,laser_id) ('+times+'))'
    return 'group by(laser) ('+latest+' and on(service_id,laser_id) (count by(service_id,laser_id) ('+latest+') == 1))'


def fleet_status_query(selector='laser!="",simulation="false"', *, tolerance=None):
    """One display series per laser, with explicit precedence and expiry.

    MetricsQL WITH keeps the expression reviewable. Controller fields are
    restricted to servo operations: monitor/agent/card completion cannot replace
    the lock state. Ambiguous concurrent controller sources remain unknown.
    The inventory persists for 30 days after telemetry disappears, but its old
    values are used only to retain an UNKNOWN row, never to claim a lock.
    """
    from ldcon.web.dashboard_state import CONTROL_OPERATIONS
    import re
    operations = '|'.join(re.escape(v) for v in CONTROL_OPERATIONS)
    # JSON strings require escaped backslashes in a regex label matcher.
    import json
    control = selector + ',operation=~' + json.dumps(operations)

    def latest(field, scope, kind):
        metric = '{__name__="ldcon_' + field + '",' + scope + ',kind="' + kind + '"}'
        raw = 'last_over_time(' + metric + '[25s])'
        if kind == 'owner_sample' and field != 'read_ok':
            health = '{__name__="ldcon_read_ok",' + scope + ',kind="owner_sample"}'
            # Optional fields omitted by the newest owner sample must not be
            # resurrected from an earlier good/disabled snapshot.
            raw = '(' + raw + ' and (tlast_over_time(' + metric + '[25s]) == tlast_over_time(' + health + '[25s])))'
        # Multiple distinct sources are not collapsed by numeric state priority.
        return '(max by(laser) (' + raw + ') and on(laser) (count by(laser) (' + raw + ') == 1))'

    definitions = dict(
        known=fleet_inventory_query(selector),
        read_ok=latest('read_ok', selector, 'owner_sample'),
        fault=latest('owner_fault', selector, 'owner_sample'),
        output=latest('output_enabled', selector, 'owner_sample'),
        owner_pi=latest('native_pi_enabled', selector, 'owner_sample'),
        trace_pi=latest('native_pi_active', control, 'trace'),
        quality=latest('frequency_valid', selector, 'owner_sample'),
        single_owner=latest('single_mode', selector, 'owner_sample'),
        single_trace=latest('single_mode', control, 'trace'),
        locked=latest('locked', control, 'trace'),
        status=latest('controller_state_code', control, 'controller_status'),
        dither_code=latest('dither_quality_code', selector+',producer="dashboard-metadata"', 'dashboard_metadata'),
        dither_until=latest('dither_quality_valid_until_unix_s', selector+',producer="dashboard-metadata"', 'dashboard_metadata'),
        dither_high='(dither_code == 2) and on(laser) (dither_until >= time())',
        pi='(owner_pi and on(laser) (read_ok == 1)) or on(laser) trace_pi',
        single='(single_owner and on(laser) (read_ok == 1) and on(laser) (quality == 1)) or on(laser) single_trace',
    )
    # Never reuse cached owner outputs after a fresh failed diagnostic, or a
    # previous good optical sample after the latest owner marks it invalid.
    branches = ['(5 + 0 * (fault == 1))', '(12 + 0 * (read_ok == 0))',
                '(13 + 0 * ((output == 0) and on(laser) (read_ok == 1)))',
                '(5 + 0 * (status == 5))',
                '(11 + 0 * (pi == 0))', '(12 + 0 * (quality == 0))',
                '(12 + 0 * (status == 0))',
                '(7 + 0 * (status == 7))',
                '(23 + 0 * (dither_high and on(laser) (pi == 1) and on(laser) ((single == 0) or on(laser) (status == 6))))',
                '(22 + 0 * (dither_high and on(laser) (pi == 1)))',
                '(6 + 0 * ((single == 0) and on(laser) (pi == 1)))']
    if tolerance is not None:
        definitions['frequency'] = latest('frequency_hz', selector, 'owner_sample')
        definitions['reference'] = configured_target_query(selector)
        definitions['near'] = '(abs(frequency - on(laser) reference) <= ' + str(tolerance) + ')'
        branches += ['(3 + 0 * ((pi == 1) and on(laser) (quality == 1) and on(laser) near and on(laser) (single == 1)))']
    branches += ['(3 + 0 * ((locked == 1) unless on(laser) (single == 0)))',
                 '(6 + 0 * (status == 6))',
                 '(status == 1 or status == 2 or status == 4 or status == 8)',
                 '(10 + 0 * (pi == 1))', '(12 + 0 * known)']
    return 'WITH (\n' + ',\n'.join('  ' + key + ' = (' + expr + ')' for key, expr in definitions.items()) + '\n)\n' + ' or on(laser) '.join(branches)


def _trace(field, card_ids, *, kind='trace'):
    import json
    import re
    cards = '|'.join(re.escape(c).replace(r'\-', '-') for c in card_ids)
    return '{__name__="ldcon_'+field+'",'+SELECTOR+',kind="'+kind+'",card=~'+json.dumps(cards)+'}'


def dlc_worker_query(field, card_ids, *, quality=False, max_sample_age_s=None):
    """Actual DLC worker snapshots, with absent newest fields kept absent.

    Readbacks use their publication time. Optical quality additionally requires
    a valid cached acquisition and a fresh frequency observation. No value is
    inferred from an output setting or a missing optional hardware parameter.
    """
    source = _trace(field, card_ids)
    stamp = _trace('observed_s', card_ids)
    raw = ('(last_over_time('+source+'[25s]) and '
           '(tlast_over_time('+source+'[25s]) == tlast_over_time('+stamp+'[25s])))')
    if quality:
        import math
        if type(max_sample_age_s) not in (int, float) or not math.isfinite(max_sample_age_s) or max_sample_age_s <= 0:
            raise ValueError('DLC quality display requires the configured sample age')
        valid = _trace('observation.quality_valid', card_ids)
        age = _trace('sample_age_s', card_ids)
        raw = ('('+raw+' and (last_over_time('+valid+'[25s]) == 1) and '
               '(last_over_time('+age+'[25s]) >= 0 <= '+str(max_sample_age_s)+'))')
    return '(max by(laser) ('+raw+') and on(laser) (count by(laser) ('+raw+') == 1))'


def dlc_trace_query(field, card_ids, *, quality=False, max_sample_age_s=None):
    """Independent owner readings first, original worker history as fallback."""
    historical = dlc_worker_query(field, card_ids, quality=quality,
                                  max_sample_age_s=max_sample_age_s)
    # Independent diagnostics continue while control cards are stopped. A
    # failed/partial newest read must not resurrect an old worker value.
    monitor = owner_metric(field)
    if quality:
        monitor = ('('+monitor+' and on(laser) ('+owner_metric('observation.quality_valid')+
                   ' == 1) and on(laser) ('+owner_metric('sample_age_s')+
                   ' >= 0 <= '+str(max_sample_age_s)+'))')
    return '('+monitor+') or on(laser) ('+historical+' unless on(laser) '+owner_metric('read_ok')+')'


def _dlc_report_queries(identifier, laser, cards):
    devices = [d for d in laser.get('devices', {}).values() if d.get('type') == 'dlcpro.laser.owner']
    if not devices:
        return None
    fields = {
        'electrical.current': ('Measured current', 'hardware.current.measured', .001),
        'control.current_bias_setpoint': ('Bias setpoint', 'hardware.current.setpoint', 1),
        'control.current_total_setpoint': ('Total setpoint', 'hardware.current.total_setpoint', 1),
        'thermal.temperature': ('Measured temperature', 'hardware.temperature.measured', 1),
        'control.temperature_setpoint': ('Temperature setpoint', 'hardware.temperature.setpoint', 1),
        'control.piezo_voltage': ('Measured PZT voltage', 'hardware.piezo.measured', 1),
        'control.piezo_setpoint': ('PZT setpoint', 'hardware.piezo.setpoint', 1),
    }
    if identifier in fields:
        label, field, scale = fields[identifier]
        expr = dlc_trace_query(field, cards)
        return [(label, expr if scale == 1 else '('+expr+') * '+str(scale))], None
    quality = {'optical.main_peak_width': [('Main fringe peak width', 'main_fwhm_fraction')],
               'optical.secondary_peaks': [('Secondary peak height', 'side_peak_height_max'),
                                           ('Secondary peak prominence', 'side_peak_prominence_max')]}
    if identifier in quality:
        if len(devices) != 1:
            return [], 'No unique DLC optical quality source'
        age = devices[0].get('control_settings', {}).get('policy', {}).get('max_sample_age_s')
        return [(label, dlc_trace_query('observation.metrics.'+field, cards,
                 quality=True, max_sample_age_s=age)) for label, field in quality[identifier]], None
    return None


def _metric_reader_binding(laser, quantity):
    """Render an existing VM binding; SQLite is deliberately not translated."""
    from ldcon.core.metric_quantities import conversion
    binding = laser.get('interfaces', {}).get('metrics', {})
    device = laser.get('devices', {}).get(binding.get('device'), {})
    if device.get('type') == 'metrics.router':
        source = device.get('control_settings', {}).get('routes', {}).get(quantity)
        device = laser.get('devices', {}).get(source, {})
    if device.get('type') != 'metrics.victoriametrics':
        return None
    definition = device.get('control_settings', {}).get('bindings', {}).get(quantity)
    if not definition:
        return None
    scale, offset = conversion(quantity, definition['source_unit'])
    query = definition['query']; expr = query['selector']
    if query.get('quality_selector'):
        # A binding promises one series; aggregate labels before combining the
        # independently named value/quality selectors.
        expr = '(max('+expr+')) and (max('+query['quality_selector']+') == 1)'
    if scale != 1:
        expr = '('+expr+') * '+repr(scale)
    if offset:
        expr = '('+expr+') + '+repr(offset)
    return expr


def report_queries(quantity, laser, context, overrides=None, *, tolerance=None):
    """Return rendering bindings for one declared quantity, or a clear absence.

    Semantic declarations live in card_reporting, while this profile owns the
    existing Telegraf/VM field paths and any explicit reader-source binding.
    """
    overrides = overrides or {}
    metrics = {**METRICS, **{k: v for k, v in overrides.items() if k in METRICS}}
    identifier = quantity['id']; cards = quantity['card_ids']
    operations = quantity['operations']
    trace = lambda field: _trace(field, cards)
    legacy_keys = {
        'optical.frequency': 'frequency',
        'control.current_axis': 'current', 'control.temperature_axis': 'temperature',
        'control.piezo_dac': 'piezo', 'control.piezo_voltage': 'piezo_voltage',
        'optical.mode_hops': 'hop', 'response.current_dither': 'dither',
        'controller.actions': 'action',
    }
    if identifier in overrides:
        return [(quantity['title'], overrides[identifier])], None
    if operations == ['acquire.metrics']:
        expr = _metric_reader_binding(laser, identifier)
        return ([(quantity['title'], expr)], None) if expr else ([], 'No Grafana-compatible VictoriaMetrics binding for this acquired quantity')
    dlc = _dlc_report_queries(identifier, laser, cards)
    if dlc is not None:
        return dlc
    old_key = legacy_keys.get(identifier)
    if old_key in overrides:
        return [(quantity['title'], overrides[old_key])], None
    if identifier in ('optical.frequency', 'optical.wavelength'):
        expr = ('hf_wsu_wavelength_nm{laser=~"${laser:regex}",channel="${wsu_channel}",cycle="${wsu_cycle}"}'
                if context['wsu_channel'] and context['wsu_cycle'] else None)
        if identifier == 'optical.frequency':
            expr = '299792458e9 / ('+expr+')' if expr else trace('frequency_hz')
        else:
            expr = expr or trace('wavelength_nm')
        return [(quantity['title'], expr)], None
    if identifier == 'controller.state':
        return [('State', fleet_status_query(SELECTOR, tolerance=tolerance))], None
    fields = {
        'control.current_setpoint': 'current_setpoint_a',
        'thermal.temperature': 'temperature_c',
        'control.temperature_setpoint': 'temperature_setpoint_c',
        'model.current_estimate': 'current_estimate_a',
        'control.current_dac': 'dac_code',
    }
    if identifier in fields:
        expr = trace(fields[identifier])
        if identifier == 'thermal.temperature':
            expr = '('+expr+') + 273.15'
        return [(quantity['title'], expr)], None
    if identifier == 'electrical.current':
        return [('Measured current', '('+trace('current_measured_a')+') or ('+trace('current_a')+')')], None
    if identifier == 'controller.actions':
        events = 'sum by(laser) (count_over_time('+_trace('action_event', cards, kind='command_applied')+'[1m]))'
        legacy = 'sum by(laser) (count_over_time('+_trace('applied', cards, kind='command_applied')+'[1m]))'
        return [('Applied commands / min', '('+events+') or on(laser) ('+legacy+')')], None
    if identifier == 'optical.mode_hops':
        return [('Confirmed hops', _trace('mode_hop', cards, kind='ecdl_response_boundary'))], None
    if identifier in ('optical.single_mode', 'optical.main_peak_width'):
        field = 'single_mode' if identifier == 'optical.single_mode' else 'fringe_metrics.main_fwhm_fraction'
        monitored = owner_metric(field)
        return [(quantity['title'], '('+monitored+') or ('+trace(field)+' unless on(laser) ('+monitored+'))')], None
    if identifier in ('control.current_axis', 'control.temperature_axis', 'control.piezo_dac'):
        field = {'control.current_axis': 'outputs.current', 'control.temperature_axis': 'outputs.temperature',
                 'control.piezo_dac': 'outputs.piezo'}[identifier]
        monitored = owner_metric(field)
        return [(quantity['title'], '('+monitored+') or ('+trace(field)+' unless on(laser) ('+monitored+'))')], None
    if identifier == 'control.piezo_voltage':
        if not context['pzt_host']:
            return [], 'No measured PZT voltage source host in the laser snapshot'
        return [('Voltage', metrics['piezo_voltage'])], None
    if identifier == 'optical.secondary_peaks':
        result = []
        for label, key in [('Secondary peak height', 'side_peak_height_max'), ('Secondary peak prominence', 'side_peak_prominence_max')]:
            field = 'fringe_metrics.'+key; monitor = owner_metric(field)
            result.append((label, '('+monitor+') or ('+trace(field)+' unless on(laser) ('+monitor+'))'))
        return result, None
    if identifier == 'response.current_dither':
        return [(label, metrics[key]) for label, key in [('Response', 'dither'), ('Warning threshold', 'dither_warning'), ('Boundary threshold', 'dither_edge')]], None
    if identifier == 'response.dither_quality':
        return [('Valid fit (1=yes)', metrics['dither_valid']), ('Source age (s)', metrics['dither_age'])], None
    return [], 'No storage/query profile for this declared quantity'



def fleet_measurement_queries(laser_selector='laser!=""'):
    """Raw wavemeter moments, independent of lock state; no resampling.

    Discover real source cycles (the synthetic cycle -1 is excluded). Refuse
    ambiguous sources. Offsets and RMS use the fresh configured target from
    the independent dashboard metadata collector, including while unlocked.
    """
    known = fleet_inventory_query(laser_selector+',simulation="false"')
    wave = 'hf_wsu_wavelength_nm{'+laser_selector+',cycle!="-1"}'
    freq = 'hf_wsu_frequency_thz{'+laser_selector+',cycle!="-1"}'

    def unique(expression, source):
        fresh = 'last_over_time('+source+'[25s])'
        return ('(max by(laser) (('+expression+') and ('+fresh+' > 0)) '
                'and on(laser) (count by(laser) ('+fresh+') == 1) and on(laser) '+known+')')

    reference = configured_target_query(laser_selector+',simulation="false"')
    latest_frequency = unique('last_over_time('+freq+'[25s])',freq)
    mean_frequency = unique('avg_over_time('+freq+'[5m])',freq)
    deviation = unique('stddev_over_time('+freq+'[5m])',freq)
    # Work in THz until the final conversion to MHz to limit cancellation.
    offset = '('+latest_frequency+' - on(laser) ('+reference+' / 1e12)) * 1e6'
    mean = '('+mean_frequency+' - on(laser) ('+reference+' / 1e12)) * 1e6'
    rms = 'sqrt(('+deviation+' * 1e6)^2 + ('+mean+')^2)'
    return dict(wavelength=unique('last_over_time('+wave+'[25s])',wave),
                wavelength_mean=unique('avg_over_time('+wave+'[5m])',wave),
                offset=offset,offset_mean=mean,rms=rms,
                reference='299792458e9 / '+reference,
                sample_age='time() - '+unique('tlast_over_time('+wave+'[25s])',wave))
