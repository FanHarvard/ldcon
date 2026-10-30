"""ESP physical writer: immutable device identity and revisioned code projections.

No laser settings, approval directory or policy files are read here. The system
compiles its current/activation/calibration envelope into native DAC codes. This
writer independently checks the actual firmware equation and physical saved caps
for every command, including FF groups and telemetry setting changes.
"""
from contextlib import contextmanager
from copy import deepcopy
import math
import threading
import uuid

from experiment_core.errors import AuthorizationRequired, StaleContext
from experiment_core.records import digest
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.owner_projection import PhysicalProjectionGuard


class NativePrewriteRefusal(BrokerError):
    """The physical owner confirms its terminal transport was never entered."""


def reachable_codes(config):
    """Device-native PI/FF reachable output, independent of laser calibration."""
    def value(key):
        result = config.get(key)
        if type(result) not in (int, float) or not math.isfinite(result):
            raise BrokerError('configuration', 'Physical configuration lacks finite ' + key)
        return float(result)
    adc = [value('mod_valid_min'), value('mod_valid_max')]
    if not 0 <= adc[0] <= adc[1] <= 65535:
        raise BrokerError('range', 'Invalid physical ADC validity interval')
    offset = value('control_offset')
    feedforward = [value('multiplier_control') * (x - value('baseline_control')) for x in adc] + [0.]
    correction = 0.
    if value('pid_multiplier'):
        if value('pid_multiplier') != 1 or value('pid_kd') != 0:
            raise BrokerError('range', 'Unsupported physical native PI mode')
        target = value('target')
        targets = [target + value('multiplier_target') * (x - value('baseline_target')) for x in adc] + [target]
        inputs = [value('system_min'), value('system_max')]
        integral = [value('pid_integral_min'), value('pid_integral_max')]
        if min(targets + inputs) <= 0 or inputs[0] > inputs[1] or integral[0] > integral[1]:
            raise BrokerError('range', 'Invalid physical PI input/integral bounds')
        correction = abs(value('pid_kp')) * max(abs(a-b) for a in targets for b in inputs)
        correction += abs(value('pid_ki')) * max(abs(x) for x in integral)
    return [offset + min(feedforward) - correction, offset + max(feedforward) + correction]


class NativeInstrumentOwner:
    """Read API plus the sole context-gated terminal ESP writer."""
    def __init__(self, driver, authority, physical_definition, *, journal):
        self._transport = driver
        self.authority = authority
        self.physical_definition = deepcopy(physical_definition)
        self._local = threading.local()
        self.guard = PhysicalProjectionGuard(driver.device_id, {driver.device_id + '/control'},
            definition_revision=digest(physical_definition), intrinsic_validator=self._validate, journal=journal)
        self.binding = self.guard.bind(authority)
        self.device_id = driver.device_id
        self.host = getattr(driver, 'host', None)
        self.capabilities = list(getattr(driver, 'capabilities', []))

    @contextmanager
    def authorized(self, authority):
        if authority is not self.authority:
            raise AuthorizationRequired('Native transport belongs to another system authority')
        authority._current()
        prior = getattr(self._local, 'authority', None)
        self._local.authority = authority
        try:
            yield
        finally:
            self._local.authority = prior

    def install(self, constraints):
        projection = self.authority.project(self.binding, constraints)
        current = self.guard.inspect(self.binding)
        if current['projection_revision'] == projection.revision:
            return current
        return self.guard.install(self.binding, projection, expected_revision=current['projection_revision'])

    def read(self):
        return self._transport.read()

    def get(self, path):
        return self._transport.get(path)

    def close(self):
        return self._transport.close()

    def _identity(self, sample):
        physical = self.physical_definition
        if sample.get('firmware_version') != physical['firmware_version']:
            raise StaleContext('Physical firmware identity changed')
        metadata = sample.get('metadata') or {}
        if metadata.get('mode') != 'normal' or metadata.get('voltage_conversion_valid') is not True:
            raise StaleContext('Physical analog mode/calibration changed')
        for name in ('adc', 'dac'):
            if any(metadata.get(name, {}).get(k) != physical[name][k] for k in ('bits', 'min_voltage', 'max_voltage')):
                raise StaleContext('Physical analog calibration changed: ' + name)

    def _validate(self, operation, constraints):
        if operation.get('kind') not in {'native_control', 'native_feedforward'}:
            raise BrokerError('request', 'Only typed native physical operations are supported')
        values = operation.get('values')
        allowed = set(constraints['allowed_settings'])
        if (not isinstance(values, dict) or not values or set(values) - allowed or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in values.values())):
            raise BrokerError('request', 'Native setting is outside the installed physical projection')
        sample = self.read()
        self._identity(sample)
        if set(values) <= {'log_adc_change_threshold'}:
            if not 0 <= values['log_adc_change_threshold'] <= 65535:
                raise BrokerError('range', 'Invalid native telemetry threshold')
            return
        candidate = {**sample['servo_config'], **values}
        codes = reachable_codes(candidate)
        low, high = constraints['code_bounds']
        limits = sample.get('dac_limits')
        if 'dac_limits_http_v1' in (sample.get('servo_status', {}).get('capabilities') or []) and not limits:
            raise BrokerError('configuration', 'Physical DAC caps are unavailable')
        if limits:
            low, high = max(low, limits['min_code']), min(high, limits['max_code'])
        if not max(0., low) <= codes[0] <= codes[1] <= min(65535., high):
            raise BrokerError('range', 'Native reachable codes exceed the effective physical projection')

    def _execute(self, operation, call, *, cleanup=False):
        if getattr(self._local, 'authority', None) is not self.authority:
            raise AuthorizationRequired('Native writes require a system-owner execution context')
        request_id = ('cleanup.' if cleanup else 'write.') + uuid.uuid4().hex
        entered = False
        def writer():
            nonlocal entered
            entered = True
            return call()
        try:
            permit = (self.authority.permit_cleanup if cleanup else self.authority.permit)(
                self.binding, operation, request_id=request_id)
            execute = self.guard.execute_cleanup if cleanup else self.guard.execute
            return execute(permit, operation, writer)
        except Exception as error:
            if not entered and not self.guard.inspect(self.binding)['fault']:
                raise NativePrewriteRefusal(getattr(error, 'code', 'configuration'), str(error)) from error
            raise

    def set_control(self, values):
        values = deepcopy(values)
        return self._execute(dict(kind='native_control', values=values), lambda: self._transport.set_control(values))

    def emergency_disable(self):
        operation = dict(kind='native_control', values={'pid_multiplier': 0})
        return self._execute(operation, lambda: self._transport.set_control({'pid_multiplier': 0}), cleanup=True)

    def set_feedforward(self, values, *, expected_revision, profile_digest):
        values = deepcopy(values)
        operation = dict(kind='native_feedforward', values=values,
                         expected_revision=expected_revision, profile_digest=profile_digest)
        return self._execute(operation, lambda: self._transport.set_feedforward(
            values, expected_revision=expected_revision, profile_digest=profile_digest))
