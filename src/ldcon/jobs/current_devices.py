"""Current interface adapter; no temperature, enable or AWG write methods."""
from dataclasses import dataclass
import time
from ldcon.core.current_settings import CurrentConditions,current_state
from ldcon.core.device_conditions import ConditionError,policy_digest
from ldcon.core.instrument_client import RemoteCLD1015,BrokerError
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.drivers.cld1015_config import configuration


@dataclass(frozen=True)
class CurrentReading:
    setpoint_a:float
    current_a:float
    observed_s:float
    values:dict
    def state(self):return self.values


class BrokerCurrentPort:
    def __init__(self,device,clock=time.monotonic):
        self.clock=clock;self.settings=device['control_settings']
        self.current_conditions=CurrentConditions(device['absolute_conditions']['current_a'],self.settings)
        c=device['connection'];self.controller=RemoteCLD1015(configuration(resource=c['resource'],endpoint=c['endpoint'],instrument=c['instrument'],
            current_limits_a=[self.current_conditions.low,self.current_conditions.high]))
        self.uncertain_output=False;self.activation=None

    def connect(self):
        try:
            self.controller.connect();description=self.controller.describe()
            if description.get('current_conditions')!=self.current_conditions.policy or description.get('control_settings')!=self.settings:raise ConditionError('Laser current policy/settings differ from instrument owner; reload broker')
            if description.get('current_writes_enabled') is not True:raise ConditionError('Current output writes are not enabled')
            return self
        except BaseException:self.controller.close();raise

    def activate(self):
        result=self.controller._call('activate_current',policy_digest=self.current_conditions.digest,settings_digest=policy_digest(self.settings))
        self.activation=result['activation'];return result

    def read_control_state(self):
        try:v=self.controller.read()
        except OSError as error:
            raise MeasurementUnavailable('Current readback temporarily unavailable: '+str(error)) from error
        except BrokerError as error:
            if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
            raise
        return CurrentReading(v['current_a'],v['current_read_a'],self.clock(),current_state(v))

    def validate_readback(self,reading):
        if self.activation is None:raise ConditionError('Current interface has not been activated')
        self.current_conditions.check(reading.state(),reading.setpoint_a,self.settings['current']['output_update_interval_s'],self.activation)

    def apply_current(self,value,elapsed_s):
        reading=self.read_control_state()
        self.current_conditions.check(reading.state(),value,elapsed_s,self.activation)
        self.uncertain_output=True
        try:result=self.controller.apply({'current_a':value})
        except BrokerError as error:
            if error.code=='measurement':
                self.uncertain_output=False
                raise MeasurementUnavailable(str(error)) from error
            raise
        self.uncertain_output=False
        return CurrentReading(result['current_a'],result['current_read_a'],self.clock(),current_state(result))

    def save_tuning(self,proposal, *, approval_token=None):
        return self.controller.save_current_tuning(policy_digest(self.settings), proposal,
                                                   approval_token=approval_token)

    def close(self):
        if not self.uncertain_output:self.controller.close()
