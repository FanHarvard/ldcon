"""Native DFB/DBR current PI: bounded bias acquisition and cache supervision."""
import math

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.owners.esp32_native_owner import C_NM, channel_metadata, current_for_code, number
from ldcon.drivers.esp32_servo import finite_number
from ldcon.core.interface_cards import _check_cancel
from ldcon.owners.esp32_native_client import OwnerUnavailable

# owner_outage_timeout_s (2026-10-27): how long the running lock tolerates an UNREACHABLE owner (restart, crash),
# retrying status/heartbeat with the same lease. The firmware PI keeps running meanwhile (rule B) and the owner
# adopts the lease when it returns. Distinct from the measurement outage (missing_timeout_s, notify-only).
DEFAULTS = dict(target_frequency_hz=None, target_wavelength_nm=None,
                tolerance_hz=1e7, duration_s=None, acquisition_timeout_s=300.,
                sample_interval_s=.5, max_sample_age_s=1., owner_outage_timeout_s=60.)


def native_trace(sample):
    """Expose reported native diagnostics; missing fields remain unavailable."""
    status, cfg = sample['servo_status'], sample['servo_config']
    raw = status.get('pid_integral', status.get('pid_integral_value'))
    bounds = [cfg.get('pid_integral_min'), cfg.get('pid_integral_max')]
    limit = max(abs(x) for x in bounds) if all(finite_number(x) for x in bounds) else None
    return dict(native_sequence=status.get('last_udp_seq'),
        native_interval_us=status.get('last_system_interval_us'),
        native_error_nm=status.get('pid_error'),native_raw_integral=raw,
        native_integral_usage=abs(raw)/limit if finite_number(raw) and limit else None,
        native_pid_output_count=status.get('pid_output'),
        native_input_quality=status.get('last_udp_quality'),
        native_config={k:cfg.get(k) for k in
            ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max',
             'pid_multiplier','control_offset','target','system_min','system_max','polling_interval')})


def parameters(card, overrides):
    if not isinstance(overrides, dict) or not isinstance(card['parameters'], dict):
        raise ValueError('Native card parameters must be mappings')
    if (set(overrides) | set(card['parameters'])) - set(DEFAULTS):
        raise ValueError('Native gains, limits and calibration belong to the device policy')
    p = {**DEFAULTS, **card['parameters'], **overrides}
    for key, value in p.items():
        if value is None and key in ('target_frequency_hz', 'target_wavelength_nm', 'duration_s'):
            continue
        number(value, key, positive=True)
    if p['target_frequency_hz'] is not None and p['target_wavelength_nm'] is not None:
        raise ValueError('Specify one frequency or wavelength target')
    if not .1 <= p['sample_interval_s'] <= 2 or p['acquisition_timeout_s'] > 3600:
        raise ValueError('Native observation timing is outside supported bounds')
    if p['owner_outage_timeout_s'] > 3600:
        raise ValueError('Native owner outage tolerance must be at most one hour')
    return p


def notify_measurement_timeout(directory, card_id, payload, outage):
    """One NON-BLOCKING measurement-outage notice (jobs v4.4, owner decision 2026-10-22): resident inbox AND deck
    Teams card, no failure case, nothing that holds reservations (measurement_notice.notify_non_blocking)."""
    from ldcon.jobs.measurement_notice import notify_non_blocking
    return notify_non_blocking(directory, card_id, 'measurement_timeout', 'outage-%d' % outage, payload)


def run_native_current(port, p, *, clock, sleep, cancel, emit, online=None, live=None, feedforward=None,
                       directory=None, laser=None, measurement_notifier=None):
    holder = []
    background = None
    if directory is not None and laser is not None:
        from ldcon.jobs.native_background_session import manager
        background = manager(directory, port, laser, clock=clock, emit=emit, poll_interval_s=p['sample_interval_s'])
    try:
        return _run_native_current(port, p, clock=clock, sleep=sleep, cancel=cancel, live=live,
                                   emit=emit, online=online, online_holder=holder, feedforward=feedforward,
                                   directory=directory, laser=laser, measurement_notifier=measurement_notifier, background=background)
    finally:
        if background is not None:
            background.close('parent_stopped')
        if holder:
            holder[0].close()


def _run_native_current(port, p, *, clock, sleep, cancel, emit, online, online_holder, live=None,
                        feedforward=None, directory=None, laser=None, measurement_notifier=None, background=None):
    if p['target_frequency_hz'] is not None:
        target_hz = p['target_frequency_hz']
    elif p['target_wavelength_nm'] is not None:
        target_hz = C_NM / p['target_wavelength_nm']
    else:
        # ONE target source (2026-10-22): the laser definition; live changes arrive through the owner's ramp.
        from ldcon.jobs.lock_target import resolve_target
        target_hz = resolve_target(laser or {}, {})
        if target_hz is None:
            raise DeviceError('Target frequency/wavelength is unset (card parameter or laser.target)')
    target_nm = C_NM / target_hz
    policy = port.policy
    started = clock()
    deadline = started + p['acquisition_timeout_s']
    prepared = False
    last_stamp = None
    missing_since = None
    owner_unreachable_since = None             # bounded owner-restart tolerance (2026-10-27)
    timeout_notified, outages = False, 0      # one measurement_timeout notice per outage (v4.4)
    locked_cycles = 0
    writes = 0
    background_refreshed = None
    point_queue = None
    if directory is not None and laser is not None:
        from ldcon.jobs.operating_point import OperatingPointQueue
        point_queue = OperatingPointQueue(directory)

    def publish_target(state, frequency, error):
        if directory is None:
            return
        from ldcon.core.records import atomic_json
        import time as _time
        from pathlib import Path
        ramp = state.get('retarget') or {}
        sample = state.get('sample') or {}
        try:
            cfg, status = sample['servo_config'], sample['servo_status']
            limit = max(abs(float(cfg['pid_integral_min'])), abs(float(cfg['pid_integral_max'])))
            integral_fraction = abs(float(status['pid_integral'])) / limit if limit > 0 else None
        except (KeyError, TypeError, ValueError):
            integral_fraction = None
        recentre = state.get('recentre') or {}
        atomic_json(Path(directory) / 'lock-target.json', dict(
            target_frequency_hz=target_hz, ramped_target_hz=ramp.get('current_hz', target_hz), ramp_active=bool(ramp.get('active')),
            frequency_hz=frequency, error_hz=error, single_mode=None, state='locked' if port.active else 'acquiring',
            integral_fraction=integral_fraction, calibration=ramp.get('calibration_note'),
            automatic_recentre=ramp.get('automatic_recentre', True), target_strategy=ramp.get('strategy'),
            target_write_verified_unix_s=ramp.get('last_write_verified_unix_s'),
            # target-ff design (2026-10-27): report-only headroom and the owner's hybrid post-step integral recentre
            dac_headroom_hz=state.get('dac_headroom_hz'),
            owner_recentre=ramp.get('recentre'), owner_recentre_check=ramp.get('recentre_check'),
            recentre_active=bool(recentre.get('active')), recentre_reason=recentre.get('reason'),
            updated_unix_s=_time.time()))

    def pause():
        end = clock() + p['sample_interval_s']
        while clock() < end:
            _check_cancel(cancel)
            sleep(max(0., min(.1, end - clock())))

    emit('state', dict(state='acquiring', native_current=True))
    while True:
        _check_cancel(cancel)
        background_ready = True
        online_busy = bool(online_holder and (online_holder[0].tuner.active or online_holder[0].tuner.finishing))
        if background is not None and port.active:
            try:
                background.poll(busy=online_busy)
                background_ready = background.foreground()
            except MeasurementUnavailable as exc:
                # Optional supervisor read loss must not end the healthy main lock.
                # Hold conflicting foreground work until the owner is readable.
                background_ready = False
                emit('background_measurement_wait',dict(reason=str(exc)))
        if live is not None and background_ready:
            # Safe boundary: between cycles, before planning the next offset.
            tuner = getattr(online_holder[0], 'tuner', None) if online_holder else None
            live.poll('between_cycles', busy=bool(getattr(tuner, 'active', False)))
            policy = port.policy
        if not port.active and clock() >= deadline:
            raise DeviceError('Native current acquisition deadline exceeded; fixed reference retained')
        if p['duration_s'] is not None and clock() - started >= p['duration_s']:
            if not port.active:
                raise DeviceError('Native card duration elapsed before a verified PI lock')
            if not locked_cycles:
                raise DeviceError('Native PI enabled but target tolerance was not reached during this finite run')
            return dict(status='succeeded', locked_cycles=locked_cycles, offset_writes=writes)
        if port.active and feedforward is not None and background_ready:
            busy = bool(online_holder and (online_holder[0].tuner.active
                                          or online_holder[0].tuner.finishing))
            attachment = feedforward(port, online_busy=busy)
            if attachment is not None:
                emit('ff_attachment_result', attachment)
                if attachment['status'] == 'review_required':
                    raise DeviceError('Native FF attachment requires review; do not replay')
                _check_cancel(cancel)
        if port.active and point_queue is not None and background_ready:
            # Operating-point request (tune.operating_point proxy): the SAME procedure as the standalone card, run
            # as this lock's own action (bounded live re-centring of the PI centre control_offset).
            from ldcon.jobs.operating_point import lock_action, NativeAdapter
            busy = bool(online_holder and (online_holder[0].tuner.active or online_holder[0].tuner.finishing))
            outcome = lock_action(point_queue, lambda: NativeAdapter(port, laser, heartbeat=port.heartbeat, max_step_codes=1, sleep=sleep), laser,
                                  clock=clock, sleep=sleep, cancel=cancel, emit=emit, lock_target_hz=target_hz, busy=busy)
            if outcome is not None:
                emit('operating_point_action', outcome)
                _check_cancel(cancel)
        try:
            port.heartbeat()
            if owner_unreachable_since is not None:
                # The owner answered with the same lease (it adopted the running PI after its restart).
                emit('owner_recovered', dict(duration_s=clock() - owner_unreachable_since, native_pi_active=port.active))
                owner_unreachable_since = None
            if not prepared:
                proof = port.mutate('prepare', target_wavelength_nm=target_nm)
                prepared = True
                emit('native_prepared', proof)
            state = port.read()
            optical = state.get('optical') or {}
            stamp = optical.get('acquired_unix_s', optical.get('acquired_s'))
            # Rule B (human decision 2026-10-22): no quality/aligned filter on wavemeter data the firmware already
            # consumes; the card's own error / "locked" report / open-loop acquisition needs a finite current reading.
            f = optical.get('frequency_hz')
            if (not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0 or optical.get('acquired_s') is None
                    or stamp is None or not 0 <= clock() - optical['acquired_s'] <= min(policy['max_sample_age_s'], p['max_sample_age_s'])):
                raise MeasurementUnavailable('Independent optical cache is stale or invalid')
            if last_stamp is not None and stamp <= last_stamp:
                pause()
                continue
            last_stamp = stamp
            frequency = optical['frequency_hz']
            ramp = state.get('retarget') or {}
            if ramp.get('final_hz') is not None and abs(float(ramp['final_hz']) - target_hz) > 1e-6:
                # the owner adopted a new laser target (tune.lock_target): follow it; error is judged against the
                # ramped value while the owner's bounded ramp runs
                emit('lock_target_changed', dict(previous_hz=target_hz, target_frequency_hz=float(ramp['final_hz'])))
                target_hz = float(ramp['final_hz'])
                target_nm = C_NM / target_hz
            judged_target = float(ramp['current_hz']) if ramp.get('active') and ramp.get('current_hz') else target_hz
            error = frequency - judged_target
            publish_target(state, frequency, error)
            sample = state['sample']
            if port.active:
                if state.get('native_pi_active') is not True:
                    raise DeviceError('Native owner no longer reports an active PI')
                if state.get('input_valid') is not True:
                    raise MeasurementUnavailable(state.get('input_error') or 'Native input unavailable')
                if abs(error) <= p['tolerance_hz']:
                    locked_cycles += 1
                emit('state', dict(state='locked' if abs(error) <= p['tolerance_hz'] else 'tracking'))
            else:
                # Native firmware's capture filter may reject a far-away initial
                # wavelength. Bounded DAC acquisition uses independent optical
                # data and never broadens that filter or borrows ECDL recovery.
                if abs(error) <= policy['native']['capture_hz']:
                    port.mutate('arm')
                    emit('state', dict(state='locked' if abs(error) <= p['tolerance_hz'] else 'tracking', native_current=True))
                else:
                    c = policy['calibration']
                    dac = channel_metadata(policy['dac'])
                    per_code = dac['voltage_step_v'] / (1 << (16-dac['bits']))
                    slope = c['wavelength_nm_per_a'] * c['current_a_per_v'] * per_code
                    previous = sample['servo_config']['control_offset']
                    delta = (target_nm - C_NM / frequency) / slope
                    from ldcon.owners.native_modulation_limits import budget_codes
                    maximum = budget_codes(policy)
                    if maximum < 1:
                        raise DeviceError('Current policy permits less than one DAC code per update')
                    step = max(-maximum, min(maximum, round(delta)))
                    if step == 0:
                        raise DeviceError('Target tolerance is below available DAC acquisition resolution')
                    value = int(previous) + step
                    # Logging must not replace the owner's final fresh prewrite gate.
                    emit('command_requested', dict(interface='native_current', dac_code=value))
                    _check_cancel(cancel)
                    port.mutate('offset', code=value, expected_code=previous)
                    writes += 1
                    emit('command_applied', dict(interface='native_current', dac_code=value,
                                                current_estimate_a=current_for_code(policy, value)))
            if missing_since is not None:
                emit('measurement_recovered', dict(duration_s=clock()-missing_since))
                if timeout_notified:
                    emit('measurement_timeout_recovered', dict(outage=outages, duration_s=clock()-missing_since))
            missing_since = None
            timeout_notified = False
            if background is not None and port.active:
                background.poll(busy=online_busy, row=background.row(state, optical, error, p['tolerance_hz']))
            if port.active and online and not (background and background.active):
                if not online_holder:
                    from ldcon.jobs.native_current_online import NativeCurrentOnline
                    online_holder.append(NativeCurrentOnline(port, online, clock))
                if (background and background.session and background.session.blocked
                        and background_refreshed != background.effect_revision
                        and not online_holder[0].tuner.active):
                    # A runtime adoption changes the next foreground trial's baseline.
                    import copy
                    adapter = online_holder[0].adapter
                    adapter.state = port.pid_state()
                    adapter.pid = copy.deepcopy(adapter.state['pid'])
                    adapter.config = copy.deepcopy(adapter.state['config'])
                    background_refreshed = background.effect_revision
                online_holder[0].cycle(optical, sample, target_hz, p['tolerance_hz'])
            emit('trace', dict(frequency_hz=frequency, wavelength_nm=C_NM/frequency,
                               error_hz=error, locked=port.active and abs(error) <= p['tolerance_hz'],
                               target_frequency_hz=target_hz,target_ramp_active=bool(ramp.get('active')),
                               acquired_s=optical['acquired_s'], source_acquired_unix_s=optical.get('acquired_unix_s'),
                               **native_trace(sample),
                               native_pi_active=port.active, dac_code=sample['dac']['native_code'],
                               current_estimate_a=current_for_code(policy, sample['dac']['native_code']),
                               current_measured_a=(state.get('bias') or {}).get('sample',{}).get('current_read_a'),
                               current_estimate_basis='CLD setpoint + nominal external-modulation transfer (with configured coefficient uncertainty); measured current logged separately'))
        except MeasurementUnavailable as exc:
            if isinstance(exc, OwnerUnavailable):
                # Owner process unreachable (restart/crash): keep the lease and keep retrying for a bounded time.
                # Nothing is written; the firmware PI runs on. Only an owner that answers with a fault or without
                # this lease (DeviceError above) ends the card.
                if owner_unreachable_since is None:
                    owner_unreachable_since = clock()
                    emit('owner_wait', dict(reason=str(exc), native_pi_active=port.active,
                                            timeout_s=p['owner_outage_timeout_s']))
                elif clock() - owner_unreachable_since >= p['owner_outage_timeout_s']:
                    raise DeviceError('Native owner unreachable for %.0f s (owner_outage_timeout_s %.0f): %s; no write '
                                      'attempted, firmware PI state unknown to this worker'
                                      % (clock() - owner_unreachable_since, p['owner_outage_timeout_s'], exc))
            elif owner_unreachable_since is not None:
                emit('owner_recovered', dict(duration_s=clock() - owner_unreachable_since, native_pi_active=port.active))
                owner_unreachable_since = None
            if online_holder:
                online_holder[0].wait(str(exc))
            if missing_since is None:
                missing_since = clock()
                emit('measurement_wait', dict(reason=str(exc), native_pi_active=port.active))
            if clock() - missing_since >= policy['missing_timeout_s'] and not timeout_notified:
                # Rule B (owner decision 2026-10-22): the host does not stop the lock or hold the PI on missing
                # observations -- the firmware keeps running on its own input. Notify (non-blocking) once per outage
                # and keep waiting; the owner's own watchdogs are unchanged.
                timeout_notified, outages = True, outages + 1
                notice = dict(outage=outages, duration_s=clock() - missing_since, reason=str(exc),
                              native_pi_active=port.active,
                              message='sr1092 native lock: no usable observation for %.0f s (%s); the lock keeps running '
                                      '(no host stop, PI not held)' % (clock() - missing_since, exc))
                delivery = None
                if measurement_notifier is not None:
                    try:
                        delivery = measurement_notifier(notice, outages)
                    except Exception as notify_error:
                        delivery = dict(routing_status='error', error='%s: %s' % (type(notify_error).__name__, notify_error))
                emit('measurement_timeout_notified', dict(notice, delivery=delivery))
        pause()
