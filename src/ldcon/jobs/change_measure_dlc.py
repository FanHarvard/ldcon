"""DLC owner binding for static current/TEC/PZT characterization, without FW writes.

Only the existing laser owner moves outputs. Its read-only plan validation also
checks coupled FF current at EVERY ordered intermediate point before any move.
"""
import copy

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.operating_point import Adapter
from ldcon.owners.dlcpro_ff import mode, preserved_currents, validate_binding


def validate_owner_moves(owner, changes):
    owner._healthy()
    if owner.mode != 'idle':
        raise DeviceError('DLC direct characterization requires idle PI and scan')
    snapshot = owner.instrument.snapshot(owner.channel, owner.lease)
    policy = owner.policy
    hardware = owner.instrument.hardware
    channel = hardware['channels'][str(owner.channel)]
    validate_binding(policy, channel)
    if snapshot['tec_fault']:
        raise DeviceError('DLC TEC fault')
    if snapshot['ff_enabled'] != policy['ff_enabled'] or snapshot['ff_factor_ma_per_v'] != policy['ff_factor_ma_per_v']:
        raise DeviceError('DLC FF context differs from policy')
    if owner.instrument.driver.read(f'laser{owner.channel}:scan:enabled') is not False:
        raise DeviceError('Native DLC scan must be stopped for static characterization')
    current = {name:snapshot[name]['setpoint'] for name in policy['axes']}
    expected = dict(current)
    for change in changes:
        axis, value = change['parameter'], change['value']
        if axis not in policy['axes']:
            raise ValueError('Unknown DLC axis')
        bound = policy['axes'][axis]['bounds']
        if not bound[0] <= value <= bound[1]:
            raise ValueError('DLC axis outside owner bounds: '+axis)
        expected[axis] = value
        if mode(policy)=='preserve_existing':
            currents = preserved_currents(policy, snapshot, [expected['piezo']], expected['current'])
        else:
            factor = policy['ff_factor_ma_per_v'] if policy['ff_enabled'] else 0.
            currents = [expected['current']+factor*(expected['piezo']-channel['ff_reference_v'])]
        for bounds in (policy['axes']['current']['bounds'], hardware['axes']['current']['bounds']):
            if any(not bounds[0] <= value <= bounds[1] for value in currents):
                raise ValueError('Ordered DLC change violates coupled FF current envelope')
    return dict(valid=True, expected=expected, outputs=current,
                ff=dict(mode=mode(policy),enabled=policy['ff_enabled'],factor_ma_per_v=policy['ff_factor_ma_per_v']),
                hardware_writes=False)


class DLCMovementAdapter(Adapter):
    locked = False
    def __init__(self, session):
        self.session = session
        self.measurement_axes = dict(axes={name:dict(port=name,locked='refused',open_loop='output',unit=p.unit)
                                          for name,p in session.policies.items()})
        state = session.call('read')
        if state['mode']!='idle' or state['fault']:
            raise DeviceError('DLC characterization requires an idle healthy owner')

    @property
    def uncertain_output(self):return self.session.uncertain_output
    def tick(self):
        state=self.session.call('heartbeat')
        if state['fault'] or state['mode']!='idle':
            raise DeviceError('DLC mode/fault changed during measurement')
    def policy(self,axis):return self.session.policies[axis]
    def readback(self,axis):return self.session.call('read')['outputs'][axis]
    def move_output(self,axis,value):return self.session.action('move',axis=axis,value=value)
    def validate_measurement_changes(self,changes):return self.session.call('validate_moves',changes=changes)
    def _measure(self, action):
        # These RPCs only read source acquisitions. A missing transition sample
        # must not turn an already acknowledged move into an unknown mutation.
        try:
            return self.session.call(action)
        except BrokerError as error:
            if error.code == 'measurement' and not self.session.uncertain_output:
                raise MeasurementUnavailable(str(error)) from error
            raise
    def measure_frequency(self):return self._measure('observe_frequency')
    def measure_observation(self):return self._measure('observe')
    def measure_context(self):return self.session.call('read')


def open_movement(laser,card):
    from ldcon.jobs.dlcpro_cards import open_ports
    session=open_ports(laser,card)['dlc_lock']
    try:
        adapter=DLCMovementAdapter(session)
    except BaseException:
        session.close()
        raise
    return {},adapter,session.close
