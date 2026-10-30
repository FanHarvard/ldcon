"""Card-owned report contracts, independent of Grafana, storage and queries.

The deck contributes quantities; the laser supplies actuator coordinates. A
renderer may select a subset without deciding that every laser has an ECDL PZT.
"""
import copy
from dataclasses import asdict

from ldcon.core.metric_quantities import QUANTITIES


def _quantity(identifier, title, title_ja, unit, meaning, constraints=''):
    return dict(id=identifier, title=title, title_ja=title_ja, unit=unit,
                meaning=meaning, constraints=constraints)


REPORT_QUANTITIES = {
    q['id']: q for q in (
        _quantity('optical.frequency', 'Optical frequency', '光周波数', 'Hz',
                  QUANTITIES['optical.frequency'].meaning, QUANTITIES['optical.frequency'].constraints),
        _quantity('optical.wavelength', 'Vacuum wavelength', '真空波長', 'nm',
                  QUANTITIES['optical.wavelength'].meaning, QUANTITIES['optical.wavelength'].constraints),
        _quantity('controller.state', 'Controller state', '制御状態', 'state',
                  'Reported controller state and, where present, fresh owner PI state.',
                  'Missing data is unknown. A PI enabled flag alone does not establish lock or mode purity.'),
        _quantity('controller.actions', 'Applied controller commands', '適用した制御指令', 'events/min',
                  'Rate of recorded command_applied events.', 'An absent event stream is not a measured zero.'),
        _quantity('electrical.current', 'Measured diode current', '測定したダイオード電流', 'A',
                  QUANTITIES['electrical.current'].meaning, QUANTITIES['electrical.current'].constraints),
        _quantity('control.current_setpoint', 'Current setpoint', '電流設定値', 'A',
                  'Controller current setpoint.', 'Not a measured current or a modulation voltage.'),
        _quantity('thermal.temperature', 'Measured temperature', '測定温度', 'K',
                  'Temperature measured at the controller sensor.', 'Not a TEC drive voltage or temperature setpoint.'),
        _quantity('control.temperature_setpoint', 'Temperature setpoint', '温度設定値', 'degC',
                  'Temperature controller setpoint.', 'Not a measured temperature.'),
        _quantity('model.current_estimate', 'Estimated modulated current', '外部変調を含む推定電流', 'A',
                  'Configured bias plus nominal external-modulation transfer applied to the native DAC.',
                  'Model estimate with calibration uncertainty; keep separate from measured current.'),
        _quantity('control.current_bias_setpoint', 'Current bias setpoint', '電流バイアス設定値', 'mA',
                  'DLC current bias setting before internal feedforward.', 'Not a measured current or total current setpoint.'),
        _quantity('control.current_total_setpoint', 'Total current setpoint', '総電流設定値', 'mA',
                  'DLC total current setting including internal feedforward.', 'Not a measured current.'),
        _quantity('control.piezo_setpoint', 'PZT voltage setpoint', 'PZT電圧設定値', 'V',
                  'DLC PZT voltage setting.', 'Not a measured voltage or a native DAC code.'),
        _quantity('control.current_dac', 'Current modulation DAC', '電流変調DAC', 'count',
                  'Native DAC code driving the current-controller external modulation input.',
                  'Not a PZT DAC or measured diode current.'),
        _quantity('control.current_axis', 'Current-controller input', '電流制御入力', 'axis unit',
                  'Readback of the declared current-control coordinate.', 'Resolve its unit from the laser; do not infer current from voltage.'),
        _quantity('control.temperature_axis', 'TEC-controller input', 'TEC制御入力', 'axis unit',
                  'Readback of the declared thermal-control coordinate.', 'Resolve its unit from the laser; a voltage is not a measured temperature.'),
        _quantity('control.piezo_dac', 'PZT control DAC', 'PZT制御DAC', 'count',
                  'Readback of the PZT control coordinate.', 'DAC counts are not measured PZT voltage.'),
        _quantity('control.piezo_voltage', 'Measured PZT voltage', '測定したPZT電圧', 'V',
                  QUANTITIES['control.piezo_voltage'].meaning, QUANTITIES['control.piezo_voltage'].constraints),
        _quantity('optical.single_mode', 'Qualified single-mode classification', '単一モード判定', '1',
                  'Single-mode classification under the configured optical calibration and quality criteria.',
                  'A classifier result, not independent proof of a physical longitudinal mode number.'),
        _quantity('optical.main_peak_width', 'Main fringe peak width', '主フリンジの幅', '1',
                  'Main peak FWHM normalized by the calibrated fringe period.',
                  'Requires qualified period and pattern; not a laser linewidth measurement.'),
        _quantity('optical.secondary_peaks', 'Position-free secondary peaks', '位置を固定しない側峰指標', '1',
                  'Largest secondary-peak height and prominence in a qualified folded fringe.',
                  'Normalized proxies, not mode power fractions. Period and pattern quality must be checked independently.'),
        _quantity('response.current_dither', 'Current dither response', '電流ディザー応答', 'nm / axis unit',
                  'Local wavelength sensitivity inferred from the configured dither and compensation response.',
                  'Valid fitted response only; not a broadband transfer function or proof of single-mode operation.'),
        _quantity('response.dither_quality', 'Dither analysis freshness', 'ディザー解析の有効性・鮮度', 'mixed',
                  'Fit validity and acquisition age of the dither source.', 'Invalid or stale fits do not produce a response value.'),
        _quantity('optical.mode_hops', 'Confirmed mode hops', '確認したモードホップ', 'events',
                  'Confirmed mode-hop event samples.', 'No data is not zero hops; hop mechanism is not inferred from this count.'),
    )
}

OPTICAL = ('optical.frequency', 'optical.wavelength')
CONTROL = ('controller.state', 'controller.actions')
ECDL_AXES = ('control.current_axis', 'control.temperature_axis', 'control.piezo_dac', 'control.piezo_voltage')
FRINGE = ('optical.single_mode', 'optical.secondary_peaks',
          'optical.main_peak_width', 'optical.mode_hops')
DITHER = ('response.current_dither', 'response.dither_quality')
DLC_OUTPUTS = ('electrical.current', 'control.current_bias_setpoint', 'control.current_total_setpoint',
               'thermal.temperature', 'control.temperature_setpoint', 'control.piezo_voltage', 'control.piezo_setpoint')
DLC_QUALITY = ('optical.secondary_peaks', 'optical.main_peak_width')
NATIVE_CURRENT = ('electrical.current', 'model.current_estimate', 'control.current_dac')

# Each operation owns its reporting contract; shared tuples do not imply shared
# device bindings. Compatibility aliases support old frozen job/deck snapshots.
CARD_REPORTS = {
    'monitor.wavelength': OPTICAL,
    'tune.temperature_once': OPTICAL + ('thermal.temperature', 'control.temperature_setpoint', 'controller.actions'),
    'servo.current_pid': OPTICAL + CONTROL + ('electrical.current', 'control.current_setpoint'),
    'servo.slow_pid': OPTICAL + CONTROL + ('electrical.current', 'control.current_setpoint'),
    'identify.current_response': OPTICAL + ('electrical.current', 'control.current_setpoint', 'controller.actions'),
    'tune.native_current': OPTICAL + CONTROL + NATIVE_CURRENT,
    'servo.dlcpro_frequency': OPTICAL + CONTROL + DLC_OUTPUTS + DLC_QUALITY,
    'sweep.dlcpro_frequency': OPTICAL + CONTROL + DLC_OUTPUTS + DLC_QUALITY,
    'servo.native_current': OPTICAL + CONTROL + NATIVE_CURRENT,
    'tune.pid_online': (),  # Historical report only.
    'tune.pid_factor_reduction': (),  # Retired public card; historical reports only.
    'tune.pid_coordinate_search': (),  # The referenced controller owns its measurements.
    'study.ecdl_mode_map': OPTICAL + ECDL_AXES + FRINGE,
    'plan.ecdl_recovery': (),  # Retired 2026-10-27 (merged into prepare.ecdl_recovery); old jobs stay readable.
    'test.ecdl_recovery': OPTICAL + CONTROL + ECDL_AXES + FRINGE,  # retired 2026-10-27, old jobs
    'prepare.ecdl_recovery': OPTICAL + CONTROL + ECDL_AXES + FRINGE,  # plan + perturbation test in one card
    'tune.ecdl_operating_point': OPTICAL + ECDL_AXES + FRINGE + ('controller.actions',),
    'recover.ecdl_mode_hop': OPTICAL + ECDL_AXES + FRINGE,
    'measure.change': OPTICAL + FRINGE + DITHER,  # Selected groups, not every group, are acquired.
    'measure.batch': OPTICAL + FRINGE + DITHER,
    'measure.target_response': OPTICAL,
    'optimize.settling_criteria': OPTICAL,
    'optimize.target_feedforward': OPTICAL,
    'optimize.target_thermal': OPTICAL + ECDL_AXES,
    'optimize.target_step': OPTICAL,
    'optimize.sweep_feedforward': OPTICAL + FRINGE,
    'tune.operating_point': OPTICAL + ('controller.actions',),   # generic; the laser's own axes are in the move rows
    'tune.lock_target': OPTICAL,
    'laser.emission': (),  # Electrical evidence is retained in emission-result.json; no optical claim.
    'tune.device_parameters': ('controller.actions',),
    'commission.ecdl_pi': OPTICAL + CONTROL + ECDL_AXES + FRINGE,
    'servo.ecdl_steady_lock': OPTICAL + CONTROL + ECDL_AXES + FRINGE + DITHER,
    'tune.ecdl_window_center': OPTICAL + ECDL_AXES + FRINGE + DITHER,
    'sweep.current_modulation': (),  # Waveform artifacts do not measure optical frequency.
    'sweep.servo_modulation': (),
    'report.grafana_dashboard': (), 'agent.resident': (), 'agent.request': (), 'notify.teams': (),
    'analyze.shot_sweep': (),  # reads finished shots' telemetry; no new measurement
    'tune.sweep_tracking': (),  # re-assesses a finished shot; no new measurement
    'acquire.metrics': (),  # The invocation declares the selected physical quantities.
}
for _old, _new in {
    'acquisition.native_current': 'tune.native_current',
    'tune.ecdl_initial_point': 'tune.ecdl_operating_point',
    'study.ecdl_thermal_context': 'study.ecdl_mode_map',
    'study.ecdl_window_center': 'tune.ecdl_window_center',
    'study.ecdl_current_response': 'tune.ecdl_window_center',
    'study.ecdl_locked_current_map': 'tune.ecdl_window_center',
}.items():
    CARD_REPORTS[_old] = CARD_REPORTS[_new]


def declarations_for(operation):
    """JSON-safe semantic metadata for the card catalog; contains no queries."""
    return [copy.deepcopy(REPORT_QUANTITIES[q]) for q in CARD_REPORTS.get(operation, ())]


def event_samples(kind, payload):
    """Publish workflow evidence through the same semantic trace/event fields.

    Raw pixels stay in workflow artifacts. These are measured points, not a
    held/interpolated claim of optical quality between acquisitions.
    """
    if kind == 'ecdl_map_observation':
        row = {key: copy.deepcopy(payload[key]) for key in
               ('frequency_hz', 'wavelength_nm', 'acquired_s', 'outputs', 'single_mode')
               if payload.get(key) is not None}
        row['fringe_metrics'] = copy.deepcopy(payload.get('metrics', {}))
        return [('trace', row)]
    confirmed = (kind == 'ecdl_recovery_test_trial' and payload.get('hop_observed') is True
        or kind == 'ecdl_mapped_recovery_candidate'
            and payload.get('samples') == payload.get('required_samples')
            and type(payload.get('samples')) is int and payload['samples'] > 0
        or kind == 'ecdl_relock_hop_candidate' and payload.get('confirmed') is True)
    if confirmed:
        return [('ecdl_response_boundary', {'mode_hop': 1, 'source_event': kind})]
    return []


def _axis_settings(laser, axis):
    binding = laser.get('interfaces', {}).get(axis, {})
    device = laser.get('devices', {}).get(binding.get('device'), {})
    return device.get('control_settings', {}).get('axes', {}).get(axis, {})


def _contextualize(quantity, laser):
    identifier = quantity['id']
    if identifier == 'thermal.temperature' and any(d.get('type') == 'dlcpro.laser.owner' for d in laser.get('devices', {}).values()):
        quantity['unit'] = 'degC'
    axis = {'control.current_axis': 'current', 'control.temperature_axis': 'temperature',
            'control.piezo_dac': 'piezo', 'response.current_dither': 'current'}.get(identifier)
    if axis:
        unit = _axis_settings(laser, axis).get('unit')
        if not unit:
            return None, 'Laser does not declare the ' + axis + ' control coordinate unit'
        quantity['unit'] = 'nm/' + unit if identifier == 'response.current_dither' else unit
        if identifier == 'control.current_axis' and unit == 'V':
            quantity['meaning'] = QUANTITIES['control.current_voltage'].meaning
        if identifier == 'control.temperature_axis' and unit == 'V':
            quantity['meaning'] = QUANTITIES['control.tec_voltage'].meaning
    return quantity, None


def collect_reports(deck, laser, selected_card_ids=None, selected_quantity_ids=None, *, card_parameters=None):
    """Collect selected deck declarations without reading devices or a database.

    Empty lists explicitly select nothing. None means every supported card or
    quantity. Unknown selections fail rather than silently becoming an ECDL view.
    """
    cards = deck.get('cards', [])
    ids = {c['id'] for c in cards}
    if selected_card_ids is not None and set(selected_card_ids) - ids:
        raise ValueError('Unknown report card IDs: ' + ', '.join(sorted(set(selected_card_ids) - ids)))
    included = ids if selected_card_ids is None else set(selected_card_ids)
    available = {}; unavailable = {}; card_rows = []
    for card in cards:
        if card['id'] not in included:
            continue
        declarations = declarations_for(card['operation'])
        if card['operation'] == 'acquire.metrics':
            declarations = []
            effective = {**card.get('parameters', {}), **(card_parameters or {}).get(card['id'], {})}
            for identifier in effective.get('quantities', ['optical.frequency']):
                if identifier in REPORT_QUANTITIES:
                    declarations.append(copy.deepcopy(REPORT_QUANTITIES[identifier]))
                elif identifier in QUANTITIES:
                    q = asdict(QUANTITIES[identifier])
                    declarations.append(dict(q, title=q['id'], title_ja=q['id']))
        quantity_ids = []
        for declaration in declarations:
            quantity, reason = _contextualize(declaration, laser)
            identifier = declaration['id']
            if reason:
                unavailable[identifier] = dict(id=identifier, reason=reason)
                continue
            if identifier not in available:
                available[identifier] = dict(quantity, card_ids=[], operations=[])
            row = available[identifier]
            row['card_ids'].append(card['id'])
            if card['operation'] not in row['operations']:
                row['operations'].append(card['operation'])
            quantity_ids.append(identifier)
        card_rows.append(dict(id=card['id'], operation=card['operation'], quantity_ids=quantity_ids))
    if selected_quantity_ids is not None and set(selected_quantity_ids) - available.keys():
        raise ValueError('Quantity is not reported by the selected deck cards: ' +
                         ', '.join(sorted(set(selected_quantity_ids) - available.keys())))
    selected = list(available) if selected_quantity_ids is None else selected_quantity_ids
    return dict(deck_id=deck.get('id'), cards=card_rows,
                available_quantities=list(available.values()),
                selected_quantities=[copy.deepcopy(available[q]) for q in selected],
                unavailable_quantities=list(unavailable.values()))
