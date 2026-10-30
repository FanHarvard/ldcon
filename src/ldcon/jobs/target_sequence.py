"""One server-side job for bounded target steps; no owner or device access.

Each step uses the ordinary target card and its verified completion. A worker
failure is not resumed or replayed. Browser disconnection does not stop a worker.
"""
import math

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.lock_target import C_NM, _finite, laser_target
from ldcon.core.target_policy import target_bounds, validate_destination


class _StepAPI:
    """Bind every read/save in a step to the sequence's current revision."""
    def __init__(self, api, revision, check):
        self.api, self.revision, self.check = api, revision, check

    def get_configuration(self, *args):
        value = self.api.get_configuration(*args)
        if value['revision'] != self.revision:
            raise CardCancelled('A newer target/configuration superseded this sequence')
        return value

    def put_configuration(self, kind, name, text, revision, **kwargs):
        self.check()
        if revision != self.revision:
            raise CardCancelled('Target sequence revision changed before save')
        value = self.api.put_configuration(kind, name, text, revision, **kwargs)
        self.revision = value['revision']
        return value


def next_step(laser, final_hz, override=None, *, reference_hz=None):
    block = laser_target(laser)
    if not block or target_bounds(laser, reference_hz=reference_hz) is None or block.get('max_change_hz') is None:
        raise DeviceError('Target sequence requires target bounds and a maximum step')
    final_hz = _finite(final_hz, 'final target', positive=True)
    try:
        validate_destination(laser, final_hz, reference_hz=reference_hz)
    except ValueError as error:
        raise DeviceError(str(error)) from error
    cap = _finite(block['max_change_hz'], 'maximum target step', positive=True)
    if override is not None:
        cap = min(cap, _finite(override, 'card maximum target step', positive=True))
    origin = float(block['frequency_hz'])
    delta = final_hz-origin
    value = final_hz if abs(delta) <= cap else origin+math.copysign(cap, delta)
    # Addition at optical frequencies may round slightly beyond the cap.
    if abs(value-origin) > cap:
        value = math.nextafter(value, origin)
    if value == origin and final_hz != origin:
        raise DeviceError('Maximum step is below frequency representation precision')
    return origin, value, cap


def live_report(lock_directory, fresh_s=30.):
    """The running lock's fresh lock-target.json row (empty when absent or stale)."""
    import time
    from pathlib import Path
    from ldcon.core.records import read_json
    row = read_json(Path(lock_directory) / 'lock-target.json', {}) or {}
    stamp = row.get('updated_unix_s')
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= fresh_s:
        return {}
    return row


def headroom_toward(live, delta_hz):
    """The owner-reported room (Hz) in the direction of ``delta_hz`` (pzt_headroom_hz / dac_headroom_hz {up_hz, down_hz});
    None when the lock reports none. A reported zero stays zero (next_step then holds until room returns)."""
    for key in ('pzt_headroom_hz', 'dac_headroom_hz'):
        room = live.get(key)
        if isinstance(room, dict):
            value = room.get('up_hz' if delta_hz >= 0 else 'down_hz')
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                return float(value)
    return None


def run_target_sequence(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None, api=None):
    from ldcon.jobs.dashboard_api import Dashboard
    from ldcon.jobs.lock_target import run_lock_target, running_lock
    api = api or Dashboard.from_job_directory(directory)
    publish = publish or (lambda value: None)
    started = clock()
    origin = laser_target(laser)
    if origin is None:
        raise DeviceError('The laser definition has no target block')
    origin = float(origin['frequency_hz'])
    final = (float(p['target_frequency_hz']) if p['target_frequency_hz'] is not None else
             C_NM/float(p['target_wavelength_nm']) if p['target_wavelength_nm'] is not None else
             origin+float(p['offset_hz']))
    current = api.get_configuration('lasers', laser['id'])
    current_target = laser_target(current['document'])
    if current_target is None or float(current_target['frequency_hz']) != origin:
        raise CardCancelled('A newer target superseded this queued sequence before it started')
    expected_revision = current['revision']
    before, destination, cap = next_step(current['document'], final, p['max_change_hz'], reference_hz=origin)
    emit('lock_target_sequence_plan', dict(before_hz=origin, final_target_hz=final,
                                         maximum_step_hz=cap, timeout_s=p['timeout_s'],
                                         admitted_bounds_hz=target_bounds(current['document'], reference_hz=origin)))
    steps = []

    def check():
        if cancel():
            raise CardCancelled('Target sequence cancelled; remaining steps were not sent')
        if clock()-started >= p['timeout_s']:
            raise DeviceError('Target sequence deadline reached; remaining steps were not sent')
        return False

    if before == final:
        return dict(status='succeeded', unchanged=True, before_hz=origin, target_frequency_hz=final,
                    revision=expected_revision, steps=[],
                    note='Final target was already saved; no new physical verification was performed')
    while True:
        check()
        remaining = p['timeout_s']-(clock()-started)
        fresh = api.get_configuration('lasers', laser['id'])
        if fresh['revision'] != expected_revision:
            raise CardCancelled('A newer target/configuration superseded this sequence; remaining steps were not sent')
        lock = running_lock(directory, fresh['document'])
        live = live_report(lock[0]) if lock is not None else {}
        # target-ff design (2026-10-27): the next step waits only while the owner's background post-step integral
        # recentre (sr1092 hybrid) is in flight, and is capped by min(saved cap, the owner's reported headroom).
        if live.get('recentre_active'):
            publish(dict(status='verifying', target_frequency_hz=final, waiting_for='owner_recentre',
                         owner_recentre=live.get('owner_recentre')))
            sleep(p['sample_interval_s'])
            continue
        override = p['max_change_hz']
        room = headroom_toward(live, final - float(laser_target(fresh['document'])['frequency_hz']))
        if room is not None and room <= 0:
            publish(dict(status='verifying', target_frequency_hz=final, waiting_for='headroom',
                         pzt_headroom_hz=live.get('pzt_headroom_hz'), dac_headroom_hz=live.get('dac_headroom_hz')))
            sleep(p['sample_interval_s'])
            continue
        if room is not None:
            override = room if override is None else min(override, room)
        before, destination, cap = next_step(fresh['document'], final, override, reference_hz=origin)
        if lock is None:
            destination = final  # The lock ended: one remaining save, no settling wait.
        step_number = len(steps)+1

        def progress(value):
            publish(dict(value, sequence=dict(step=step_number, completed_steps=len(steps),
                step_target_hz=destination, final_target_hz=final, maximum_step_hz=cap)))

        progress(dict(status='queued', target_frequency_hz=destination))
        result = run_lock_target(directory, fresh['document'], card,
            dict(p, queue_steps=False, target_frequency_hz=destination, target_wavelength_nm=None,
                 offset_hz=None, max_change_hz=cap, timeout_s=remaining),
            clock=clock, sleep=sleep, cancel=check, emit=emit, publish=progress,
            api=_StepAPI(api, expected_revision, check))
        if result.get('status') != 'succeeded' or not result.get('revision'):
            raise DeviceError('Target step was not confirmed; remaining steps were not sent')
        steps.append(dict(step=step_number, **result))
        expected_revision = result['revision']
        emit('lock_target_sequence_step_completed', steps[-1])
        # Never overwrite a newer request, even after the last step's receipt.
        if api.get_configuration('lasers', laser['id'])['revision'] != expected_revision:
            raise CardCancelled('A newer target/configuration superseded this sequence')
        if destination == final:
            completed = dict(result, before_hz=origin, target_frequency_hz=final, steps=steps)
            progress(dict(status='settled' if result.get('locked') else 'saved', target_frequency_hz=final, completed_steps=len(steps)))
            return completed
