"""Return an acknowledged partial-sweep correction after verified ARC cleanup.

Successful zero-ending sweeps retain their drift correction. A failed sweep can
leave a large correction appropriate only to its last nonzero plateau. Return
that correction only in the original, healthy context; never chase a new target
or continue after an ambiguous write.
"""
import math

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_awg_tracking_guard import verify_base


def restore_if_needed(sweep, sample):
    o, s, tracking = sweep.o, sweep.session, sweep.tracking
    if (tracking is None or s['stop_verdict'] != 'failed'
            or abs(sample.frequency_hz-o.target) <= o.control['capture_hz']):
        return sample
    s['base_return'] = dict(state='checking', writes=[])

    def check():
        state = o.instrument._state(o.channel, o.lease)
        if (o.fault or s.get('cancel_requested') or not s.get('resume_allowed', True)
                or o.mode != 'awg_sweep' or o.target_revision != s['origin_target_revision']
                or digest(o.policy) != tracking.revision or state['revision'] != tracking.revision
                or o.control != tracking.control
                or o.clock()-o.last_client > o.policy['client_timeout_s']):
            raise MeasurementUnavailable('Base return skipped: original controller context unavailable')
        held = o.instrument.confirmed_outputs[o.channel]
        if any(held[a] != tracking.origin['hardware'][a]['setpoint']
               for a in ('current', 'temperature')):
            raise MeasurementUnavailable('Base return skipped: output anchors changed')
        # No capture test here: the known residual is why return is necessary.
        # Existing source freshness and absolute input-window checks still apply.
        return sweep._resume_frequency()

    check()
    origin = tracking.proof['tracking']['origin_base_v']
    before = o.instrument.confirmed_outputs[o.channel]['piezo']
    verify_base(before, tracking.proof)
    verify_base(origin, tracking.proof)
    # No new safety setting. Small increments use the existing calibrated
    # capture scale and output cadence; the whole path was admitted with ARC=0.
    step = o.control['capture_hz']/abs(o.policy['axes']['piezo']['frequency_hz_per_unit'])
    count = max(1, math.ceil(abs(origin-before)/step))
    s['base_return'].update(state='returning', origin_base_v=origin, initial_base_v=before)
    for index in range(1, count+1):
        with o.mutex, o.instrument.mutex, o.instrument.driver.control_transaction():
            check()
            value = before+(origin-before)*index/count
            verify_base(value, tracking.proof)
            actual = o.instrument._write(o.channel, o.lease,
                o.instrument.driver.path(o.channel, 'pc:voltage-set'), value,
                o.policy['axes']['piezo']['readback_tolerance'], compact=True, prewrite=check)
            verify_base(actual, tracking.proof)
        row = dict(requested_v=value, actual_v=actual, write_timing=dict(o.instrument.last_write_timing))
        s['base_return']['writes'].append(row)
        o.audit(dict(event='awg_base_return', request_id=s['request']['request_id'], **row))
        sweep.sleep(o.policy['axes']['piezo']['update_interval_s'])
    s['base_return']['state'] = 'verified'
    s['base_restored'] = True
    return check()
