"""One verified target + calibrated PZT feedforward boundary.

The internal DLC current FF stays enabled with its existing coefficient. Unknown
writes fault the owner; there is no replay or guessed target restoration.
"""
import math
import time
from ldcon.drivers.dlcpro import UncertainDLCWrite
from ldcon.core.contracts import MeasurementUnavailable


def apply(owner, target):
    o=owner;o._healthy()
    delta_hz=target-o.target
    if not math.isfinite(target) or target<=0 or abs(delta_hz)>o.control['capture_hz']:
        raise ValueError('Target step exceeds the configured capture increment')
    o.read_control()
    before=o.instrument.snapshot(o.channel,o.lease)
    slope=o.policy['axes']['piezo']['frequency_hz_per_unit']
    delta=delta_hz/slope
    output=before['outputs']['piezo'] if 'outputs' in before else before['piezo']['setpoint']
    value=output+delta
    started=o.clock();started_unix=time.time()
    def before_write():
        fresh=o.read_control()
        if abs(fresh.frequency_hz-o.target)>o.control['capture_hz']:
            raise MeasurementUnavailable('Current target left capture before FF write; no write attempted')
        if o.clock()-o.last_pi_write<o.policy['axes']['piezo']['update_interval_s']:
            raise MeasurementUnavailable('PZT update interval not yet elapsed; no write attempted')
    try:
        confirmed=o.instrument.move(o.channel,o.lease,'piezo',value,prewrite=before_write)
    except UncertainDLCWrite:
        o.fault='Target feedforward write outcome uncertain'
        o.audit(dict(event='owner_fault',reason=o.fault));raise
    o.base+=confirmed-output
    o.target=target;o.target_revision+=1
    fence=o.clock();o.last_acquisition=fence
    o.applied_pzt=confirmed
    o.last_pi_write=fence
    result=dict(confirmed=True,target_frequency_hz=target,target_revision=o.target_revision,
        output_before_v=output,output_after_v=confirmed,delta_v=delta,delta_frequency_hz=delta_hz,
        started_s=started,confirmed_s=fence,started_unix_s=started_unix,confirmed_unix_s=time.time(),acquisition_fence_s=fence,ff_scale=1.)
    o.audit(dict(event='target_step',**result))
    return result
