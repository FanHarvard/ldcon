"""Family-specific settings inside the existing exclusive attached trial epoch.

No second instrument lease is created. Caller holds the laser-owner mutex, so
PZT/TEC commands cannot interleave with a verified current/FF transaction.
Current and thermal trials restore their own starting commands, not an old
optical target. Thermal settling is recorded separately from command handover.
"""
from copy import deepcopy
import math

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_ff import mode
from ldcon.owners.dlcpro_ff_delta_adjustment import adjust_factor_qualified_delta
from ldcon.owners.dlcpro_owner import inside

FIELDS = {
    'current': {'current_ma': 'mA'},
    'slow_loop': {'tec_gain': '1', 'tec_period_s': 's', 'pzt_center_v': 'V'},
    'feedforward': {'ff_factor_ma_per_v': 'mA/V'},
}


def settings(owner, family):
    if family == 'current':
        value = owner.instrument.confirmed_outputs.get(owner.channel, {}).get('current')
        if value is None:raise ValueError('Confirmed current command unavailable')
        return dict(current_ma=value)
    if family == 'slow_loop':
        return {key: owner.control[key] for key in FIELDS[family]}
    if family == 'feedforward':
        return dict(ff_factor_ma_per_v=owner.policy['ff_factor_ma_per_v'])
    raise ValueError('Unsupported DLC parameter family')


def qualification(owner):
    record = getattr(owner, 'ff_adjustment_evidence', None)
    if not isinstance(record, dict) or set(record) != {'evidence', 'tolerance_ma'}:
        raise ValueError('Commissioned FF delta evidence is unavailable')
    value = record['tolerance_ma']
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid commissioned FF hold tolerance')
    evidence = record['evidence']
    identity = evidence['identity']
    if any(not isinstance(identity.get(k), str) or not identity[k] for k in ('serial_number','firmware_version')):
        raise ValueError('Pinned FF chassis and firmware are required')
    bounds = evidence['pzt_hardware_bounds_v']
    if (not isinstance(bounds, list) or len(bounds) != 2
            or any(type(x) not in (float, int) or not math.isfinite(x) for x in bounds)
            or bounds[0] >= bounds[1]):raise ValueError('Invalid qualified PZT hardware bounds')
    delta = evidence['pzt_delta_qualification']
    if (delta.get('verified') is not True or type(delta.get('channel')) is not int
            or delta.get('channel') != owner.channel
            or not isinstance(delta.get('source'), str) or not delta['source']):
        raise ValueError('Qualified FF PZT-delta evidence required')
    hardware = owner.instrument.hardware['channels'][str(owner.channel)]
    if hardware.get('ff_delta_verified') is not True or owner.policy.get('ff_enabled') is not True:
        raise ValueError('Enabled commissioned internal FF is required')
    return deepcopy(record)


def capabilities(owner):
    result = {}
    for family, fields in FIELDS.items():
        reason = None
        try:
            settings(owner, family)
            if family == 'feedforward':qualification(owner)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            reason = str(error)
        result[family] = dict(supported=reason is None, reason=reason, fields=list(fields), units=dict(fields),
                             adoption='runtime_only', optical_target_changes=False)
    return result


def validate(owner, family, values):
    if family not in FIELDS or not isinstance(values, dict) or set(values) != set(FIELDS[family]):
        raise ValueError('Candidate fields differ from the selected DLC family')
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values.values()):
        raise ValueError('DLC parameter candidates must be finite numbers')
    if family == 'current':
        inside(values['current_ma'], owner.policy['axes']['current']['bounds'])
    elif family == 'slow_loop':
        owner._check_control({**owner.control, **values})
    else:
        qualification(owner)
        inside(values['ff_factor_ma_per_v'], owner.policy['ff_factor_bounds_ma_per_v'])
    return deepcopy(values)


def _adjust_ff(owner, factor, *, restoring=False):
    proof = qualification(owner)
    owner._healthy()
    before = owner.instrument.snapshot(owner.channel, owner.lease)
    old = owner.policy['ff_factor_ma_per_v']
    if factor == old:
        return dict(confirmed=True, readback=dict(ff_factor_ma_per_v=old), output_write=False)
    anchor = dict(piezo_v=before['piezo']['setpoint'],
                  temperature_c=before['temperature']['setpoint'],
                  total_current_ma=before['current']['total_setpoint'])
    try:
        receipt = adjust_factor_qualified_delta(owner.instrument, owner.channel, owner.lease,
            factor, evidence=proof['evidence'], anchor=anchor, tolerance_ma=proof['tolerance_ma'],
            expected_factor=old, preserve_existing=mode(owner.policy)=='preserve_existing',
            prewrite=None if restoring else owner.read_control)
    except Exception:
        state = owner.instrument.channels.get(owner.channel, {})
        if state.get('fault'):
            owner.fault = 'FF trial output requires inspection; no rollback'
        raise
    owner.policy['ff_factor_ma_per_v'] = factor
    owner.instrument.confirmed_outputs[owner.channel]['current'] = receipt['after']['current']['setpoint']
    # The old hidden FF reference is deliberately not inferred or persisted.
    return dict(confirmed=True, readback=dict(ff_factor_ma_per_v=factor), output_write=True,
                ff_transaction=receipt, policy_revision=digest(owner.policy))


class ParameterTrialSettings:
    def __init__(self, owner, family, ff_evidence=None):
        self.owner, self.family = owner, family
        if ff_evidence is not None:
            # Only an owner-attached commissioning record may supply authority.
            if ff_evidence != getattr(owner, 'ff_adjustment_evidence', None):
                raise ValueError('FF evidence differs from the owner-adopted record')
        if family not in FIELDS:raise ValueError('Unsupported DLC parameter family')
        snapshot = owner.instrument.snapshot(owner.channel, owner.lease)
        if (snapshot['ff_enabled'] != owner.policy['ff_enabled']
                or snapshot['ff_factor_ma_per_v'] != owner.policy['ff_factor_ma_per_v']):
            raise DeviceError('Physical FF context differs from owner-adopted settings')
        self.checkpoint = validate(owner, family, settings(owner, family))
        self.checkpoint_temperature = owner.instrument.confirmed_outputs[owner.channel]['temperature']
        self.checkpoint_current = owner.instrument.confirmed_outputs[owner.channel]['current']
        self.ff_path = [dict(settings=deepcopy(self.checkpoint), current_ma=self.checkpoint_current)]

    def settings(self):return settings(self.owner, self.family)

    def validate(self, values):return validate(self.owner, self.family, values)

    def switch(self, values, *, restoring=False):
        o = self.owner
        o._healthy()
        values = self.validate(values)
        before = self.settings()
        if not restoring:o.read_control()
        if self.family == 'feedforward':
            result = _adjust_ff(o, values['ff_factor_ma_per_v'], restoring=restoring)
            if not restoring and values != before:
                self.ff_path.append(dict(settings=deepcopy(values),
                    current_ma=o.instrument.confirmed_outputs[o.channel]['current']))
        elif self.family == 'current':
            if values == before:result = dict(output_write=False)
            else:
                try:
                    got = o.instrument.move(o.channel, o.lease, 'current', values['current_ma'],
                                            prewrite=None if restoring else o.read_control)
                except Exception:
                    if o.instrument.channels.get(o.channel, {}).get('fault'):
                        o.fault = 'Current trial output requires inspection; no rollback'
                    raise
                if abs(got-values['current_ma']) > o.policy['axes']['current']['readback_tolerance']:
                    o.fault = 'Current trial readback mismatch'
                    raise DeviceError(o.fault)
                result = dict(output_write=True)
        else:
            o.control.update(values)
            result = dict(output_write=False, thermal_settled=False)
        result.update(confirmed=True, before=before, readback=self.settings(),
                      target_revision=o.target_revision, family=self.family)
        o.audit(dict(event='parameter_trial_switch', **result))
        return result

    def restore(self):
        o = self.owner
        o._healthy()
        if self.family == 'feedforward':
            receipts = []
            while len(self.ff_path)>1:
                previous = self.ff_path[-2]
                receipt = _adjust_ff(o, previous['settings']['ff_factor_ma_per_v'], restoring=True)
                # The matched baseline includes the current offset, not merely
                # the FF slope. PZT may have moved during the comparison.
                if o.instrument.confirmed_outputs[o.channel]['current'] != previous['current_ma']:
                    try:
                        o.instrument.move(o.channel,o.lease,'current',previous['current_ma'])
                    except Exception:
                        if o.instrument.channels.get(o.channel,{}).get('fault'):
                            o.fault='FF baseline current restoration requires inspection'
                        raise
                receipt['current_offset_ma']=o.instrument.confirmed_outputs[o.channel]['current']
                receipts.append(receipt)
                self.ff_path.pop()
            result=dict(confirmed=True,readback=self.settings(),receipts=receipts,
                        current_offset_ma=o.instrument.confirmed_outputs[o.channel]['current'])
        else:
            result = self.switch(self.checkpoint, restoring=True)
        if self.family == 'slow_loop':
            now = o.instrument.confirmed_outputs[o.channel]['temperature']
            if now != self.checkpoint_temperature:
                try:
                    returned = o.instrument.move(o.channel, o.lease, 'temperature', self.checkpoint_temperature)
                except Exception:
                    if o.instrument.channels.get(o.channel, {}).get('fault'):
                        o.fault = 'TEC trial restoration requires inspection; no replay'
                    raise
                if abs(returned-self.checkpoint_temperature) > o.policy['axes']['temperature']['readback_tolerance']:
                    o.fault = 'TEC trial restoration readback mismatch'
                    raise DeviceError(o.fault)
                result['output_write'] = True
            result.update(temperature_setpoint=self.checkpoint_temperature, thermal_settled=False)
        result['settings'] = self.settings()
        return result

    def commit(self):
        self.owner._healthy()
        if self.family == 'feedforward':
            if not hasattr(self.owner, 'runtime_ff_restore'):
                self.owner.runtime_ff_restore = deepcopy(self.checkpoint)
                self.owner.runtime_ff_restore_path = [deepcopy(self.checkpoint)]
            self.owner.runtime_ff_restore_path.extend(deepcopy(row['settings']) for row in self.ff_path[1:])
        return dict(confirmed=True, settings=self.settings(), adoption='runtime_only',
                    restores_on_lock_stop=self.family=='feedforward')


def restore_ff_on_stop(owner):
    """Return adopted runtime FF to the saved startup context before idle/release."""
    original = getattr(owner, 'runtime_ff_restore', None)
    if original is None:return None
    # Keep the verified adoption increments: several individually bounded
    # coefficient changes cannot be collapsed into one larger current excursion.
    path = owner.runtime_ff_restore_path
    receipts = []
    while len(path)>1:
        receipt = _adjust_ff(owner, path[-2]['ff_factor_ma_per_v'], restoring=True)
        receipts.append(receipt)
        path.pop()  # Only a confirmed transition advances this restoration.
    result = dict(confirmed=True, readback=settings(owner,'feedforward'), receipts=receipts)
    del owner.runtime_ff_restore
    del owner.runtime_ff_restore_path
    owner.audit(dict(event='runtime_ff_restored_on_stop', **result))
    return result
