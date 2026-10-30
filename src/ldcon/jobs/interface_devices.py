"""Typed driver resolution; only interfaces used by the selected card are opened."""
import time
from ldcon.core.device_conditions import TemperatureConditions,ConditionError,policy_digest
from ldcon.core.interface_cards import TemperatureReading
from ldcon.drivers.fizeau import HttpFizeauWavemeter
from ldcon.core.instrument_client import RemoteCLD1015
from ldcon.drivers.cld1015_config import configuration


class BrokerTemperaturePort:
    def __init__(self,device,clock=time.monotonic):
        self.clock=clock;self.conditions=TemperatureConditions(device['absolute_conditions']['temperature_c'],device.get('control_settings'))
        from ldcon.core.temperature_settings import coefficient
        self.frequency_hz_per_c=coefficient(device.get('control_settings'))
        conn=device['connection']
        self.controller=RemoteCLD1015(configuration(resource=conn['resource'],endpoint=conn['endpoint'],instrument=conn['instrument'],
            current_limits_a=None,temperature_limits_c=[self.conditions.low,self.conditions.high]))
        self.uncertain_output=False

    def connect(self):
        try:
            self.controller.connect()
            description=self.controller.describe()
            if description['policy_digest']!=self.conditions.digest:raise ConditionError('Laser and instrument-owner absolute conditions differ; reload the broker policy')
            if any('settings.' in str(x) for x in [self.conditions.policy['max_slew_c_per_s'],*self.conditions.policy['expressions']]) and description.get('control_settings')!=self.conditions.settings:raise ConditionError('Shared device settings differ from instrument owner')
            expected_temperature=self.conditions.settings.get('temperature')
            if expected_temperature is not None and description.get('control_settings',{}).get('temperature')!=expected_temperature:raise ConditionError('Temperature device settings differ from instrument owner')
            if description.get('temperature_writes_enabled') is not True:raise ConditionError('Instrument owner has not enabled output writes')
            bounds=description.get('temperature_limits_c')
            if not bounds or not bounds[0]<=self.conditions.low<self.conditions.high<=bounds[1]:raise ConditionError('Laser temperature envelope exceeds instrument-owner envelope')
            return self
        except BaseException:
            self.controller.close();raise

    def _reading(self,value):
        return TemperatureReading(value['temperature_c'],value['temperature_read_c'],self.clock(),
                                  value['tec_enabled'],value['ld_enabled'],value['tec_current_a'],value['current_read_a'])

    def read_temperature(self):return self._reading(self.controller.read())

    def ramp_temperature(self,value_c,duration_s):
        before=self.read_temperature();self.conditions.check(before.state(),value_c,duration_s)
        self.uncertain_output=True
        result=self.controller.apply({'temperature_c':value_c},ramp_s=duration_s)
        self.uncertain_output=False
        return self._reading(result)

    def close(self):
        # A timed-out write may have executed. Retain the broker lease for review.
        if not self.uncertain_output:self.controller.close()


def open_interfaces(laser,card,*,clock=time.monotonic):
    from ldcon.jobs.response_algorithms import ALGORITHMS
    if card['operation'] in ALGORITHMS:return {}
    if card['operation']=='identify.axis_frequency':
        from ldcon.jobs.axis_frequency import open_ports
        return open_ports(laser,card)
    if card['operation'] in ('servo.dlcpro_frequency','sweep.dlcpro_frequency'):
        from ldcon.jobs.dlcpro_cards import open_ports
        return open_ports(laser,card)
    if card['operation'].startswith('sweep.'):
        from ldcon.jobs.native_ff_proxy import feedforward_route
        from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route
        if feedforward_route(laser,card['uses'][0]) or rig_locked_route(laser,card['uses'][0]):
            return {}  # the proxy owns no instrument; the lock job's session (native FF / rig owner) does the device work
    if card['operation']=='acquire.metrics':
        from ldcon.jobs.metric_interface import open_metric_interface
        return {'metrics':open_metric_interface(laser)}
    if card['operation'] in ('servo.native_current','tune.native_current'):
        from ldcon.owners.esp32_native_client import NativeCurrentPort
        binding=laser['interfaces']['native_current']
        return {'native_current':NativeCurrentPort(laser['devices'][binding['device']]).connect()}
    if card['operation'].startswith('sweep.'):
        from ldcon.jobs.modulation_sweep import SweepPort
        return {'modulation':SweepPort(laser,card['uses'][0])}
    if card['operation'] in ('servo.ecdl_steady_lock','commission.ecdl_pi'):
        from ldcon.jobs.ecdl_devices import open_steady_interfaces
        return open_steady_interfaces(laser,card)
    if card['operation']=='tune.ecdl_window_center':
        # fast_side_peak standalone (no running lock): the rig owner's open-loop session (rule F)
        from ldcon.jobs.ecdl_devices import open_ecdl_interfaces
        from ldcon.core.interface_schema import ECDL_OPEN_PORTS
        return open_ecdl_interfaces(laser,dict(card,uses=list(ECDL_OPEN_PORTS)))
    if card['operation'] in ('measure.change','measure.batch','tune.operating_point','tune.lock_target','tune.device_parameters','laser.emission'):
        return {}  # rule F generic cards open the laser's own session (unlocked) or file into the running lock job
    if card['operation'] in ('tune.ecdl_initial_point','study.ecdl_current_response','study.ecdl_thermal_context',
                             'study.ecdl_mode_map','test.ecdl_recovery','prepare.ecdl_recovery','tune.ecdl_operating_point','recover.ecdl_mode_hop','tune.ecdl_branch_hop'):
        # recover.ecdl_mode_hop was missing here (2026-10-22): the standalone card fell through to 'No typed driver'.
        from ldcon.jobs.ecdl_devices import open_ecdl_interfaces
        return open_ecdl_interfaces(laser,card)
    result={}
    try:
        for name in card['uses']:
            binding=laser['interfaces'][name];device=laser['devices'][binding['device']];conn=device['connection']
            if binding['capability'] in ('frequency_monitor','scalar_sensor') and device['type']=='fizeau.owner':
                from ldcon.jobs.optical_broker import BrokerFrequencyPort
                result[name]=BrokerFrequencyPort(device)
                if binding['capability']=='scalar_sensor':
                    from ldcon.jobs.slow_current import FrequencyScalarSensor
                    result[name]=FrequencyScalarSensor(result[name])
            elif binding['capability'] in ('frequency_monitor','scalar_sensor') and device['type']=='fizeau.http':
                result[name]=HttpFizeauWavemeter(conn['endpoint'],channel=conn['channel'],cycle=conn['cycle'],
                    trust_source_clock=conn.get('trust_source_clock',False),clock=clock,expected_laser=conn.get('expected_laser'))
                if binding['capability']=='scalar_sensor':
                    from ldcon.jobs.slow_current import FrequencyScalarSensor
                    result[name]=FrequencyScalarSensor(result[name])
            elif binding['capability']=='temperature_controller' and device['type']=='thorlabs.cld1015.broker':
                result[name]=BrokerTemperaturePort(device,clock).connect()
            elif binding['capability']=='current_controller' and device['type']=='thorlabs.cld1015.broker':
                from ldcon.jobs.current_devices import BrokerCurrentPort
                result[name]=BrokerCurrentPort(device,clock).connect()
            elif binding['capability']=='slow_actuator' and device['type']=='thorlabs.cld1015.broker' and binding.get('axis')=='current':
                from ldcon.jobs.current_devices import BrokerCurrentPort
                from ldcon.jobs.slow_current import CurrentSlowActuator
                result[name]=CurrentSlowActuator(BrokerCurrentPort(device,clock).connect())
            else:raise ValueError('No typed driver for selected interface')
        return result
    except BaseException:
        for port in result.values():
            if hasattr(port,'close'):port.close()
        raise
