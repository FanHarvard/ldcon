"""DLC Pro 1.9/3.2 USER console adapter; no connection is opened on import.

Command reference pp. 38-43, 51-61, 118-119, 262. Exactly one console
connection belongs to the instrument owner. Never retry a write after timeout.
"""
import json
import math
import re

from ldcon.core.contracts import DeviceError


from ldcon.drivers.dlcpro_console import Console, UncertainDLCWrite


class DLCParameterError(DeviceError):
    def __init__(self, path, code):
        super().__init__(f'DLC parameter {path} returned error {code}')
        self.path, self.code = path, code


def _name(path):
    if not isinstance(path, str) or not re.fullmatch(r'[a-z][a-z0-9:-]*', path):
        raise ValueError('Invalid DLC parameter name')
    return path


def _atom(value):
    if type(value) is bool:
        return '#t' if value else '#f'
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('DLC writes require finite numbers or booleans')
    return repr(value)


def parse_atom(text):
    text = text.strip()
    if text in ('#t', '#f'):
        return text == '#t'
    if text.startswith('"') and text.endswith('"'):
        return json.loads(text)
    try:
        value = float(text)
    except ValueError:
        raise DeviceError('Unexpected DLC scalar reply') from None
    if not math.isfinite(value):
        raise DeviceError('Nonfinite DLC scalar reply')
    return value


class DLCPro:
    """Unit-preserving parameter access. There is deliberately no eval/exec RPC."""
    AXES = {'piezo': ('pc:voltage-set', 'pc:voltage-act', 'V'),
            'temperature': ('tc:temp-set', 'tc:temp-act', 'degC'),
            'current': ('cc:current-offset', 'cc:current-act', 'mA')}

    def __init__(self, console):
        self.console = console
        self.unavailable_parameters = {}
        self.compact_write_supported = False

    @staticmethod
    def path(channel, suffix):
        if type(channel) is not int or channel not in (1, 2, 3, 4):
            raise ValueError('Invalid DLC laser channel')
        return _name(f'laser{channel}:dl:{suffix}')

    def read(self, path):
        path = _name(path)
        reply = self.console.command(f"(param-ref '{path})")
        error = re.fullmatch(r'Error: (-?\d+)(?: .*)?', reply)
        if error:
            raise DLCParameterError(path, int(error[1]))
        return parse_atom(reply)

    def read_optional(self, path):
        """Only an explicit unknown/unavailable parameter means unsupported.

        Transport failure, malformed data, and access denial still propagate.
        The negative capability cache lives only for this connection.
        """
        if path in self.unavailable_parameters:
            return None
        try:
            return self.read(path)
        except DLCParameterError as error:
            if error.code not in (-3, -20):
                raise
            self.unavailable_parameters[path] = error.code
            return None

    def identity(self, channel):
        self.path(channel, 'serial-number')  # Validate before building names.
        return dict(system_type=self.read('system-type'),
                    serial_number=self.read('serial-number'),
                    firmware_version=self.read('fw-ver'),
                    product_name=self.read(f'laser{channel}:product-name'),
                    head_serial_number=self.read(self.path(channel, 'serial-number')))

    def verify_identity(self, serial_number, firmware_version=None):
        identity = dict(serial_number=self.read('serial-number'),
                        firmware_version=self.read('fw-ver'))
        if identity['serial_number'] != serial_number:
            raise DeviceError('DLC chassis identity mismatch')
        if firmware_version is not None and identity['firmware_version'] != firmware_version:
            raise DeviceError('DLC firmware differs from commissioned version')
        return identity

    def control_transaction(self):
        return self.console.lock

    def verify_compact_write_support(self):
        # Constants only: this startup capability check reads/writes no outputs.
        reply = self.console.command('(let ((status 0)) (list status 12.5))')
        if self._pair(reply) != (0., 12.5):
            raise DeviceError('DLC ordered result/readback expression unsupported')
        self.compact_write_supported = True

    @staticmethod
    def _pair(reply):
        match = re.fullmatch(r'\(\s*([^\s()]+)\s+([^\s()]+)\s*\)', reply.strip())
        if not match:
            raise DeviceError('DLC verification pair malformed')
        return parse_atom(match[1]), parse_atom(match[2])

    def write(self, path, value, *, tolerance=0., compact=False):
        # The acknowledgement and readback form one indivisible transaction.
        # Low-priority monitoring may not interleave between them.
        with self.console.lock:
            return self._verified_write(path, value, tolerance=tolerance, compact=compact)

    def _verified_write(self, path, value, *, tolerance, compact):
        path = _name(path)
        # Any response other than exact success needs review. Code 2 is CLIPPED,
        # not success; do not accept even if its subsequent readback happens to match.
        if compact:
            if not self.compact_write_supported:
                raise DeviceError('Compact write capability was not verified')
            # let forces param-set! to finish before param-ref. Merely putting
            # both calls in list arguments would leave Scheme evaluation order unspecified.
            expression = f"(let ((status (param-set! '{path} {_atom(value)}))) (list status (param-ref '{path})))"
        else:
            expression = f"(param-set! '{path} {_atom(value)})"
        reply = self.console.command(expression, writing=True)
        try:
            status, actual = self._pair(reply) if compact else (parse_atom(reply), None)
            if type(status) is bool or status != 0:
                raise ValueError('DLC rejected, clipped or warned on write')
            if not compact:
                actual = self.read(path)
            if type(value) is bool:
                equal = actual is value
            else:
                equal = type(actual) is not bool and abs(actual - value) <= tolerance
            if not equal:
                raise ValueError('DLC setpoint readback mismatch')
        except Exception as error:
            raise UncertainDLCWrite('DLC write not verified; inspection required') from error
        return actual

    def snapshot(self, channel):
        values = {}
        for axis, (setting, measured, unit) in self.AXES.items():
            values[axis] = dict(setpoint=self.read(self.path(channel, setting)),
                                measured=self.read(self.path(channel, measured)), unit=unit)
        values['current']['total_setpoint'] = self.read(self.path(channel, 'cc:current-set'))
        values['piezo']['calculated_output'] = self.read_optional(self.path(channel, 'pc:voltage-out'))
        values['piezo']['calculated_output_available'] = values['piezo']['calculated_output'] is not None
        values['feedforward_master'] = self.read_optional(f'laser{channel}:feedforward-master')
        values['unavailable_parameters'] = dict(self.unavailable_parameters)
        values['emission'] = self.read(self.path(channel, 'cc:emission'))
        values['pzt_enabled'] = self.read(self.path(channel, 'pc:enabled'))
        values['tec_enabled'] = self.read(self.path(channel, 'tc:enabled'))
        values['tec_fault'] = self.read(self.path(channel, 'tc:fault'))
        values['ff_enabled'] = self.read(self.path(channel, 'cc:feedforward-enabled'))
        values['ff_factor_ma_per_v'] = self.read(self.path(channel, 'cc:feedforward-factor'))
        return values
