"""Compile a triangle card through the same checked route as the sweep API.

No I/O. ``dwell_s`` retains its waveform meaning: one endpoint-to-endpoint
ramp, hence period = 2 * dwell_s. The configured laser clock and vertex grid
are authoritative. Stop/restoration remain the existing owner protocol.
"""


def card_parameters(laser, parameters):
    if parameters.get('shape') != 'triangle' or parameters.get('shot_sweep') or parameters.get('calibrate_only'):
        return parameters
    from ldcon.web.sweep_api import Route, parse_triangle, RESTORE_MARGIN_S
    from ldcon.web.lock_target_api import ApiError

    p = dict(parameters)
    try:
        route = Route(laser)
        names = ('ff_gain_scale',) if route.kind == 'native' else ('ldc_ff_v_per_hz', 'pzt_ff_gain_scale')
        options = route.options({key: p[key] for key in names if key in p})
        low, high = sorted(p['levels_hz'])
        seq = parse_triangle(route, dict(shape='triangle', low_hz=low, high_hz=high,
                                        period_s=2 * p['dwell_s'], max_run_s=p['duration_s']), options)
    except ApiError as error:
        raise ValueError(str(error)) from None
    # The shared compiler uses linear interpolation across these vertices, not
    # plateaus. This is the same representation used by /sweeps/continuous.
    p.update(shape='staircase', levels_hz=seq['levels'], dwell_s=seq['dwell_s'], **options)
    p['_waveform_shape'] = 'triangle'
    if parameters.get('trigger', 'auto') in ('auto', 'continuous'):
        p.update(trigger='continuous', duration_s=seq['max_run_s'] + RESTORE_MARGIN_S)
    if route.kind == 'native':
        p.update(edge_s=seq['dwell_s'])
    else:
        p.update(edge_s=min(.01, seq['dwell_s']), lead_s=seq['dwell_s'] / 2,
                 tail_s=seq['dwell_s'] / 2, sample_rate_hz=route.settings['awg']['max_sample_rate_hz'])
    return p
