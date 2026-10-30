"""sr422: locked staircase through the ECDL rig owner, proxied by the frequency-staircase card job.

The sweep.servo_modulation card on a laser whose servo_modulation interface is the ECDL rig (ecdl.rig.owner
with control_settings.locked_sweep) starts its own job (labscript polls and stops THAT id), files a request
into the running servo.ecdl_steady_lock job (native_ff_proxy.RequestQueue + at-most-once journal) and mirrors
the owner's measured state on its own shot_sweep. The steady-lock worker runs the session at a cycle boundary
under its own rig lease, through the owner RPCs of instruments runtime 2026-10-22 (ecdl_locked_sweep):
  locked_sweep_calibrate  CH2 two-point DC step with zero multipliers (ADC counts/V, analog-path check)
  locked_sweep_prepare    CH1 LDC FF staircase instead of the dither + CH2 servo staircase, both MAN/EXT bursts
                          idle at the centre; firmware target modulation + PZT FF; TEC open; outer loop frozen
  locked_sweep_status     owner samples (wavemeter + firmware residual per sample), READY, playing/done
  locked_sweep_trigger    software trigger, once only (both channels in one socket write)
  locked_sweep_restore    waits for a playing record to end, restores firmware/CH1 dither/CH2, measured settle
The owner also restores by itself on a lost session heartbeat (10 s), sustained |error|, mode loss or a
foreign firmware change. Stop on the proxy restores only that shot; the lock keeps running.
"""
import copy
import math
import statistics
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.jobs.native_ff_proxy import RequestQueue, digest, run_locked_proxy

SCHEMA = 'ldcon.locked-staircase/v1'
TRIGGERS = ('software', 'auto', 'external', 'continuous')
OWNER_TRIGGER = {'software': 'software', 'auto': 'software', 'external': 'external', 'continuous': 'continuous'}
CALIBRATION_REFUSALS = ('No valid CH2/ADC calibration in this owner process', 'CH2/ADC calibration is older than')
OWNER_CAP_REASONS = ('continuous burst completed', 'continuous lifetime reached')
FINAL = ('restored', 'restored_unverified', 'review_required')


def rig_locked_route(laser, role='servo_modulation'):
    """True when this modulation role is served by the ECDL rig owner with an explicit locked_sweep configuration."""
    binding = laser.get('interfaces', {}).get(role)
    if not binding:
        return False
    device = laser['devices'].get(binding['device'], {})
    return device.get('type') == 'ecdl.rig.owner' and isinstance(device.get('control_settings', {}).get('locked_sweep'), dict)


def locked_settings(laser, role='servo_modulation'):
    binding = laser['interfaces'][role]
    return laser['devices'][binding['device']]['control_settings']['locked_sweep']


def validate_settings(settings):
    """Card-side shape check; the owner validates every envelope again at prepare."""
    for key in ('revision', 'awg', 'ext_mod', 'firmware', 'ldc_ff', 'limits'):
        if key not in settings:
            raise ValueError('locked_sweep.%s is required' % key)
    if settings['awg'].get('channel') != 2:
        raise ValueError('locked_sweep.awg.channel must be 2 (servo ADC on the current AWG CH2)')
    return settings


def plan_from_card(p, shot):
    """Owner plan body (trigger/identity are added by the lock-side session)."""
    if p.get('calibrate_only'):
        return dict(schema=SCHEMA, calibrate_only=True)
    if p.get('shape') == 'triangle' and not shot:
        raise ValueError('Locked rig route plays staircases only')
    if shot and shot.get('unit') != 'Hz':
        raise ValueError('Locked rig route takes frequency levels (unit Hz)')
    levels = [float(v) for v in (shot['levels'] if shot else p['levels_hz'])]
    plan = dict(schema=SCHEMA, levels_hz=levels,
                dwell_s=float(shot['dwell_s'] if shot else p['dwell_s']),
                lead_s=float(shot['lead_s'] if shot else p.get('lead_s') or 0.),
                tail_s=float(shot['tail_s'] if shot else p.get('tail_s') or 0.),
                settle_s=float(shot.get('settle_s', 0.) if shot else 0.),
                acquire_s=float(shot.get('acquire_s', 0.) if shot else 0.),
                sample_rate_hz=float(p['sample_rate_hz']),
                ldc_ff_v_per_hz=float(p.get('ldc_ff_v_per_hz') or 0.),
                pzt_ff_gain_scale=float(p.get('pzt_ff_gain_scale') or 1.))
    if p.get('full_data_log_ms') is not None:
        plan['full_data_log_ms'] = p['full_data_log_ms']
    if p.get('_waveform_shape') == 'triangle':
        plan['waveform_shape'] = 'triangle'
    if plan['lead_s'] <= 0 or plan['tail_s'] <= 0:
        raise ValueError('Locked rig route needs lead_s > 0 and tail_s > 0 (approach/leave ramps from the centre)')
    return plan


class RigRoute:
    lock_operation = 'servo.ecdl_steady_lock'
    axis = 'servo_modulation'

    def __init__(self, laser):
        self.laser = laser

    def settle_budget_s(self):
        """v1.4: the owner's settle window (limits.settle_timeout_s) + margin, for outcome.settle.deadline_unix_s."""
        limits = locked_settings(self.laser).get('limits') or {}
        value = limits.get('settle_timeout_s')
        return (float(value) if isinstance(value, (int, float)) else 30.) + 5.

    def settings_revision(self):
        return digest(locked_settings(self.laser))

    def make_plan(self, p, shot):
        validate_settings(locked_settings(self.laser))
        return plan_from_card(p, shot)


def run_rig_proxy(directory, laser, card, p, *, clock, sleep, cancel, emit, publish):
    return run_locked_proxy(directory, laser, card, p, RigRoute(laser), clock=clock, sleep=sleep, cancel=cancel, emit=emit, publish=publish)


def residual_table(records, levels):
    """Per commanded level (identified from the firmware's effective target, i.e. the ADC): n samples, mean and
    max |residual| of the firmware time-matched error (pid_error, the wavemeter value the PI consumed) and of the
    wavemeter-frame error. All samples of a level are kept; at 10 Hz sampling and 0.1 s dwell expect n ~ 1."""
    by = {}
    for row in records:
        index = row.get('level_index')
        if index is None:
            continue
        by.setdefault(index, []).append(row)
    table = []
    for index in sorted(by):
        rows = by[index]
        fw = [r['fw_error_hz'] for r in rows if r.get('fw_error_hz') is not None]
        wm = [r['error_hz'] for r in rows if r.get('error_hz') is not None]
        table.append(dict(level_index=index, level_hz=levels[index] if index < len(levels) else None, n=len(rows),
                          fw_mean_hz=statistics.mean(fw) if fw else None, fw_max_abs_hz=max(map(abs, fw)) if fw else None,
                          wm_mean_hz=statistics.mean(wm) if wm else None, wm_max_abs_hz=max(map(abs, wm)) if wm else None,
                          single_mode=all(r.get('single_mode') is not False for r in rows)))
    return table


def _without_samples(tracking):
    if isinstance(tracking, dict) and isinstance(tracking.get('degradation'), dict) and 'samples' in tracking['degradation']:
        return dict(tracking, degradation={k: v for k, v in tracking['degradation'].items() if k != 'samples'})
    return tracking


def hardware_ok(st):
    """The owner's restore checks all passed (firmware, CH1, CH2, reserved channels)."""
    checks = (st.get('restore') or {}).get('checks') or {}
    return all(checks.get(k) is True for k in ('firmware', 'ch1', 'ch2', 'reserved'))


class RigCall:
    """Owner RPCs through the steady-lock proxy. Only owner fault/io outcomes keep the session's uncertain flag."""

    def __init__(self, lock):
        self.lock = lock

    def __call__(self, action, **values):
        from ldcon.core.instrument_client import BrokerError
        session = getattr(self.lock, 'session', None)
        try:
            if action in ('locked_sweep_prepare', 'locked_sweep_calibrate') and getattr(session, 'lease', None) is not None:
                # prepare includes the owner's CH2 preflight (one full record, up to ~20 s at +-600 MHz): longer reply budget
                from ldcon.core.contracts import MeasurementUnavailable
                try:
                    return session.rpc(action, _timeout=120, lease=session.lease, **values)
                except BrokerError as error:
                    if error.code == 'measurement':
                        raise MeasurementUnavailable(str(error)) from error
                    raise
            return self.lock._call(action, False, **values)
        except BrokerError as error:
            if session is not None and error.code in ('fault', 'io', 'server'):
                session.uncertain_output = True
            raise


# ---------------------------------------------------------------- lock-side session (inside the steady-lock worker)
class ProxiedRigSweepSession:
    def __init__(self, lock, request, queue, *, clock=time.monotonic, sleep=time.sleep, cancel=lambda: False,
                 emit=lambda *_: None, poll_s=.2):
        self.lock, self.request, self.queue = lock, request, queue
        self.rpc = RigCall(lock)
        self.clock, self.sleep, self.emit, self.poll_s = clock, sleep, emit, poll_s
        self.card_cancel = cancel
        trigger = request.get('trigger', 'software')
        if trigger == 'external_rising':
            trigger = 'external'
        if trigger not in TRIGGERS:
            raise ValueError('Locked rig route supports software, auto, external and continuous triggers (got %s)' % trigger)
        self.trigger = trigger
        self.seen = 0
        self.records = []
        self.state = dict(state='preparing', armed=False, ready=False, restored=False, trigger_mode=trigger,
                          trigger_source={'software': 'MAN', 'auto': 'MAN', 'external': 'EXT', 'continuous': 'MAN'}[trigger],
                          fired=False, fired_unix_s=None, fire_ack_unix_s=None, awg_trigger_ack=None,
                          trigger_requested_unix_s=None, trigger_forwarded_unix_s=None, playback_started_unix_s=None,
                          request_id=request['request_id'], plan_sha256=request.get('plan_sha256'), measured=None, errors=[],
                          samples=0, protocol=2)   # v1.4: '.finish' after the record, 'settling' at the verified restore

    def cancelled(self):
        return bool(self.card_cancel()) or self.queue.cancelled(self.request['request_id'])

    def _publish(self, **update):
        self.state.update(update)
        self.queue.write_status(self.request['request_id'], self.state)
        self.emit('sweep_proxy_state', copy.deepcopy({k: (_without_samples(v) if k == 'tracking' else v)
                                                      for k, v in self.state.items() if k != 'residuals'}))

    def _status(self):
        self.lock.heartbeat()
        return self._absorb(self.rpc('locked_sweep_status', since=self.seen))

    def _absorb(self, st):
        rows = st.get('records') or []
        for row in rows:
            self.emit('sweep_sample', row)
        self.records.extend(rows)
        fdl = (st.get('programmed') or {}).get('full_data_log')
        changed = fdl is not None and fdl != getattr(self, 'full_data_log', None)
        if fdl is not None:
            self.full_data_log = fdl
        stats = getattr(self, 'tracking_stats', None)
        degradation = getattr(self, 'degradation_log', None)
        if stats is not None:
            for row in rows:
                stats.add(row.get('commanded_offset_hz'), row.get('dac'), None, row.get('fw_error_hz'))
                if degradation is not None:
                    degradation.add(row)
            if rows or changed:
                self.state['tracking'] = self._tracking_summary(samples=getattr(self, 'tracking_final', False))
        if isinstance(st.get('degradation'), dict):
            self.owner_degradation = st['degradation']          # owner v5.7+: counts incl. pre-trigger samples
        self.seen = st.get('record_count', self.seen + len(rows))
        self.state['samples'] = self.seen
        return st

    def _tracking_summary(self, *, samples):
        """TrackingStats summary + the playback's mode degradation (v3.16/v4.25, human 2026-10-23 ~12:10Z). Live
        publishes carry the degradation summary only; the per-sample list is added once, before the restore (the outcome
        is computed from the 'settling' publish)."""
        out = self.tracking_stats.summary()
        out['full_data_log'] = getattr(self, 'full_data_log', None)
        if getattr(self, 'degradation_log', None) is not None:
            out['degradation'] = self.degradation_log.summary(samples=samples)
            out['owner_degradation'] = getattr(self, 'owner_degradation', None)
        return out

    def _fire(self, record):
        ack = self.rpc('locked_sweep_trigger')
        self._publish(state='fired', fired=True, fired_unix_s=ack.get('fired_unix_s'), fire_ack_unix_s=ack.get('fire_ack_unix_s'),
                      awg_trigger_ack=dict(ok=bool(ack.get('ok')), source=ack.get('source'), command=ack.get('command'), readback=ack.get('readback')),
                      trigger_requested_unix_s=(record or {}).get('requested_unix_s'), trigger_forwarded_unix_s=(record or {}).get('forwarded_unix_s'))
        self.emit('sweep_trigger_fired', dict(trigger=self.trigger, ack=ack, record=record))

    def calibrate(self):
        result = self.rpc('locked_sweep_calibrate')
        self.emit('sweep_calibration', result)
        if not result.get('ok'):
            self._publish(state='rejected', errors=list(result.get('reasons', [])), calibration=result)
            return dict(status='rejected', errors=list(result.get('reasons', [])), calibration=result, restored=True)
        self._publish(state='restored', restored=True, calibration=result, result_status='succeeded')
        return dict(status='succeeded', calibration=result, restored=True)

    def run(self):
        plan = self.request['plan']
        if plan.get('calibrate_only'):
            try:
                return self.calibrate()
            except Exception as exc:
                self._publish(state='review_required' if getattr(self.lock.session, 'uncertain_output', False) else 'rejected',
                              errors=[str(exc)])
                return dict(status='review_required' if getattr(self.lock.session, 'uncertain_output', False) else 'rejected',
                            errors=[str(exc)], restored=not getattr(self.lock.session, 'uncertain_output', False))
        result = dict(status='failed', errors=[], restored=False)
        prepared = False
        continuous = self.trigger == 'continuous'
        lifetime = float(self.request.get('max_run_s') or self.request.get('lifetime_s', 600.)) if continuous \
            else float(self.request.get('lifetime_s', 600.))
        owner_plan = dict({k: v for k, v in plan.items() if k != 'calibrate_only'}, trigger=OWNER_TRIGGER[self.trigger],
                          request_id=self.request['request_id'], plan_sha256=self.request.get('plan_sha256') or digest(plan),
                          lifetime_s=lifetime)
        last_owner = None
        try:
            if self.cancelled():
                raise InterruptedError('Sweep proxy cancelled before prepare')
            try:
                prep = self._prepare(owner_plan)
            except Exception as exc:
                from ldcon.core.instrument_client import BrokerError
                if isinstance(exc, BrokerError) and exc.code in ('request', 'busy', 'configuration', 'range', 'measurement'):
                    # A clean owner refusal leaves the rig as it was: refused before any write, or (preflight / armed
                    # idle check) with CH2 restored and verified and nothing else written. Uncertain states latch an
                    # owner fault instead (code 'fault'). labscript's abort check needs restored:true here (2026-10-22).
                    errors=[str(exc), 'pre-shot state confirmed: the owner refused cleanly (nothing written, or CH2 restored and verified)']
                    result.update(status='rejected', errors=errors, restored=True)
                    self._publish(state='rejected', errors=errors, restored=True)
                    return result
                raise
            prepared = True
            self._publish(state='armed', armed=True, programmed=prep.get('programmed'))
            deadline = self.clock() + lifetime
            fired = False
            done = False
            playing_seen = False
            periods = None
            self.applied_proposals = []
            while self.clock() < deadline:
                if done and self.queue.finished(self.request['request_id']):
                    # v1.4: the proxy ends a shot whose record completed (a normal end, not a cancel)
                    self._publish(finished_by_proxy=True)
                    break
                if self.cancelled():
                    if continuous and playing_seen:
                        # v1.2: the stop is the normal end of a continuous run; the owner restores at the next period
                        # boundary (the centre)
                        self._publish(stop_reason='stop_requested')
                        break
                    raise InterruptedError('Sweep proxy stopped')
                st = self._status()
                owner_state = st.get('state')
                if owner_state != last_owner:
                    self.emit('sweep_owner_state', dict(state=owner_state, abort_reason=st.get('abort_reason')))
                    last_owner = owner_state
                if owner_state in FINAL or owner_state in ('settling', 'restoring', 'restore_pending'):
                    if continuous and playing_seen and any(r in str(st.get('abort_reason')) for r in OWNER_CAP_REASONS):
                        self._publish(stop_reason='owner_cap', periods_played=st.get('periods_played'))
                        break
                    raise RuntimeError('Owner ended the sweep: ' + str(st.get('abort_reason') or owner_state))
                if owner_state == 'armed' and st.get('ready') and self.state['state'] in ('armed', 'ready'):
                    if self.state['state'] != 'ready':
                        self._publish(state='ready', ready=True, measured=st.get('measured'))
                    if not fired and self.trigger in ('auto', 'continuous'):
                        self._fire(None); fired = True
                    elif not fired and self.trigger == 'software':
                        record = self.queue.trigger_record(self.request['request_id'])
                        if record is not None:
                            self._fire(record); fired = True
                if owner_state == 'playing' and self.state['state'] != 'playing':
                    playing_seen = True
                    self._publish(state='playing', playback_started_unix_s=st.get('playback_started_unix_s'))
                    try:                        # human 2026-10-23 morning: tracking samples over the playback
                        from ldcon.jobs.sweep_tracking import TrackingStats
                        programmed = self.state.get('programmed') or {}
                        mc, hpc = programmed.get('multiplier_control'), programmed.get('hz_per_count')
                        self.tracking_stats = TrackingStats(
                            ff_codes_per_hz=(float(mc) / float(hpc)) if isinstance(mc, (int, float)) and hpc else None,
                            gain_scale=float(plan.get('pzt_ff_gain_scale', 1.)))
                        from ldcon.jobs.sweep_tracking import DegradationLog
                        self.degradation_log = DegradationLog(st.get('playback_started_unix_s'))
                        for row in self.records:          # rows of this status already absorbed (owner rows carry phase)
                            self.degradation_log.add(row)
                    except Exception:           # noqa: BLE001
                        self.tracking_stats = None
                if continuous and owner_state == 'playing':
                    context = st.get('tec_context')
                    if st.get('periods_played') != periods or context != getattr(self, '_published_context', None):
                        # v3.14: publish whenever the period count OR the TEC context window changes (the lock loop is
                        # blocked during the triangle, so these are the only live TEC-following outputs)
                        if context != getattr(self, '_published_context', None):
                            self._published_context = context
                            self.emit('tec_follow_context', dict(context=context, periods_played=st.get('periods_played'),
                                                                 proposal=st.get('tec_proposal'), tec_moves=st.get('tec_moves')))
                        periods = st.get('periods_played')
                        self._publish(periods_played=periods, period_s=st.get('period_s'), context=context,
                                      tec_moves=st.get('tec_moves'))
                    self._tec_follow(st)
                if owner_state == 'done' and not done and not continuous:
                    done = True
                    plan_levels = plan['levels_hz']
                    self._publish(state='done', residuals=residual_table(self.records, plan_levels))
                    if self.trigger == 'auto':
                        break
                waiting_trigger = self.trigger == 'software' and not fired and self.state['state'] == 'ready'
                self.sleep(.02 if waiting_trigger else self.poll_s)
            else:
                if not (continuous and playing_seen):
                    raise RuntimeError('Sweep proxy lifetime expired before the shot was stopped')
                self._publish(stop_reason='max_run_s')
            result['status'] = 'succeeded'
        except InterruptedError as exc:
            result['status'] = 'cancelled'
            result['errors'].append(str(exc))
        except Exception as exc:
            result['errors'].append(str(exc))
        finally:
            if getattr(self, 'tracking_stats', None) is not None:
                try:                            # the per-sample degradation list, once, before the outcome publish
                    self.tracking_final = True
                    self.state['tracking'] = self._tracking_summary(samples=True)
                except Exception:               # noqa: BLE001 - reporting never blocks the restore
                    pass
            if prepared:
                self._finish_restore(result)
            else:
                result['restored'] = result['status'] != 'failed' or not getattr(self.lock.session, 'uncertain_output', False)
            if not result['restored'] and result['status'] != 'rejected':
                result['status'] = 'review_required'
            final = 'restored' if result['restored'] else 'review_required'
            self._publish(state=final if result['status'] != 'rejected' else 'rejected', restored=bool(result['restored']),
                          errors=result['errors'], result_status=result['status'],
                          residuals=None if self.trigger == 'continuous' else residual_table(self.records, plan.get('levels_hz', [])))
            self.emit('sweep_session_result', {k: v for k, v in result.items()})
        return result

    def _tec_follow(self, st):
        """v1.2 continuous (human 2026-10-23: TEC following stays ON during the triangle). The lock loop is blocked in
        this session, so the session applies the owner's TEC proposal itself: owner v5.4 publishes in locked_sweep_status
        at most ONE 'PZT slow centering' TEC proposal computed on the integer-period context DAC (mean of dac - FF term
        over whole periods aligned to the trigger). Each proposal is sent ONCE, exactly as published, with steady_adjust
        (the owner routes it to its bounded TEC move during a playing continuous triangle). Only the temperature axis;
        current is never dispatched. Refusals/measurement gaps skip it (the owner plans again on the next window)."""
        import json
        if st.get('tec_context') is not None:
            self.state['context'] = st['tec_context']
        proposal = st.get('tec_proposal')
        if not isinstance(proposal, dict):
            return
        key = json.dumps(proposal, sort_keys=True, default=str)
        if key in self.applied_proposals:
            return
        self.applied_proposals = (self.applied_proposals + [key])[-50:]
        if proposal.get('axis') != 'temperature':
            self.emit('tec_follow_refused', dict(action=proposal, reason='only TEC moves during the triangle'))
            return
        try:
            result = self.lock.adjust(proposal)
        except (DeviceError, MeasurementUnavailable) as exc:
            if getattr(getattr(self.lock, 'session', None), 'uncertain_output', False):
                raise
            self.emit('tec_follow_deferred', dict(action=proposal, reason=str(exc)[:200]))
            return
        self.emit('tec_follow_applied', dict(action=proposal, result=result))

    def _prepare(self, owner_plan):
        """locked_sweep_prepare; v3.11 (2026-10-23, after the instruments restart): when the owner refuses only because
        this owner process has no (or a too old) CH2/ADC calibration, run the owner's existing locked_sweep_calibrate
        ONCE (CH2 only, multipliers 0, analog-path check, CH2 back to its exact snapshot) and retry prepare ONCE. The
        result is published as 'calibrated'. A failed calibration returns the ORIGINAL refusal (rejected, nothing else)."""
        from ldcon.core.instrument_client import BrokerError
        ready = self.request.get('ready') or {}
        try:
            return self.rpc('locked_sweep_prepare', plan=owner_plan, ready=ready)
        except BrokerError as refusal:
            if refusal.code != 'request' or not any(m in str(refusal) for m in CALIBRATION_REFUSALS):
                raise
            try:
                calibration = self.rpc('locked_sweep_calibrate')
            except (BrokerError, MeasurementUnavailable) as error:
                if isinstance(error, BrokerError) and error.code in ('fault', 'io', 'server'):
                    raise                              # an uncertain owner state is never hidden behind the refusal
                self.emit('sweep_auto_calibration', dict(ok=False, error=str(error)[:300]))
                raise refusal from error
            ok = bool(isinstance(calibration, dict) and calibration.get('ok'))
            self.emit('sweep_auto_calibration', dict(ok=ok, result=calibration))
            self._publish(calibrated=dict(ok=ok, reason=str(refusal)[:200], result=calibration))
            if not ok:
                raise refusal
            return self.rpc('locked_sweep_prepare', plan=owner_plan, ready=ready)

    def _finish_restore(self, result):
        self._publish(state='restoring')
        try:
            self.lock.heartbeat()
            st = self._absorb(self.rpc('locked_sweep_restore', reason='; '.join(result['errors'])[:200] or 'shot finished', since=self.seen))
            deadline = self.clock() + float(st.get('duration_s') or 0.) + 60.
            settling_sent = False
            while st.get('state') not in FINAL and self.clock() < deadline:
                if st.get('state') == 'settling' and not settling_sent and hardware_ok(st):
                    # v1.4: the owner verified the hardware restore (firmware, CH1, CH2, reserved channels) -> the
                    # shot's result exists now; the owner's settle check below only follows it
                    settling_sent = True
                    self._publish(state='settling', hardware_restored=True, result_status=result['status'],
                                  errors=list(result['errors']) + (['owner abort: ' + str(st['abort_reason'])] if st.get('abort_reason') else []),
                                  settle_started_unix_s=time.time(), restore_checks=(st.get('restore') or {}).get('checks'))
                self.sleep(self.poll_s)
                st = self._status()
            restore = st.get('restore') or {}
            self.state['measured_after'] = st.get('measured_after')
            result['restore'] = restore
            result['owner_state'] = st.get('state')
            if st.get('abort_reason'):
                result['errors'].append('owner abort: ' + str(st['abort_reason']))
            result['restored'] = st.get('state') == 'restored'
            result['hardware_restored'] = st.get('state') in ('restored', 'restored_unverified') and hardware_ok(st)
            self.state['hardware_restored'] = result['hardware_restored']
            if st.get('state') == 'restored_unverified':
                result['errors'].append('hardware restored and verified, but the lock did not meet the settle criteria')
                if result['hardware_restored'] and 'not armed' not in str(restore.get('reason')):
                    # v1.4 (as sr1092 since 2026-10-22): a settle miss after a verified hardware restore is a REPORTED
                    # result, not a review case (the lock continues). 'native PI not armed' stays a review case.
                    result['restored'] = True
                    if result['status'] == 'succeeded':
                        result['status'] = 'completed_unsettled'
        except Exception as exc:
            result['errors'].append('Restore: ' + str(exc))
            result['restored'] = False


def attachment(lock_directory, laser, *, card_cancel, emit, clock=time.monotonic, sleep=time.sleep):
    """The ``sweep`` hook for run_steady_lock on a rig-locked-route laser (called once per cycle with the lock proxy)."""
    from ldcon.jobs.native_ff_sweep import NativeFFAttachment, RequestJournal
    queue = RequestQueue(lock_directory)
    journal = RequestJournal(Path(lock_directory) / 'ff-requests' / 'journal', Path(lock_directory).name, digest(locked_settings(laser)))

    class Hook(NativeFFAttachment):
        def __call__(self, lock, *, online_busy=False):
            request = self.receive()
            if request is None:
                return None
            try:
                path = self.journal.claim(request, now=self.wall_clock())
            except ValueError as exc:
                queue.write_status(request['request_id'], dict(state='rejected', errors=[str(exc)], armed=False, ready=False, restored=False))
                return dict(status='rejected', errors=[str(exc)], request_id=request['request_id'])
            if online_busy:
                result = dict(status='rejected', errors=['Another online operation (window/tuner/recovery) is active'], restored=True)
            elif request.get('route') not in (None, 'servo.ecdl_steady_lock'):
                result = dict(status='rejected', errors=['Request is not for the ECDL rig route'], restored=True)
            else:
                try:
                    result = ProxiedRigSweepSession(lock, request, queue, clock=clock, sleep=sleep, cancel=card_cancel, emit=emit).run()
                except Exception as exc:
                    result = dict(status='review_required', errors=[str(exc)])
                    queue.write_status(request['request_id'], dict(state='review_required', errors=[str(exc)], armed=False, ready=False, restored=False))
            if result.get('status') == 'rejected':
                queue.write_status(request['request_id'], dict(state='rejected', errors=result.get('errors', []), armed=False, ready=False,
                                                               restored=bool(result.get('restored')), calibration=result.get('calibration')))
            self.journal.finish(path, result)
            if result.get('status') == 'review_required' and getattr(getattr(lock, 'session', None), 'uncertain_output', False):
                raise DeviceError('Locked sweep left an uncertain owner state; review required: ' + '; '.join(result.get('errors', [])))
            return dict(result, request_id=request['request_id'])

    return Hook(queue.receive, journal, lambda: None, clock=clock, sleep=sleep, cancel=card_cancel, emit=emit)
