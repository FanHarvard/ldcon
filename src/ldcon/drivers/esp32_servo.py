"""Shared esp32servocon monitor driver, configured by host rather than laser."""

import json
import math
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


NATIVE_BITS = 16  # Firmware protocol: fixed AD5541/AD7680 SPI word size.


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('Device HTTP redirects are not allowed')


def finite_number(value):
    return type(value) in (float, int) and math.isfinite(value)


def channel_metadata(value):
    if not isinstance(value, dict):
        raise ValueError('Missing analog channel metadata')
    bits, low, high = (value.get(key) for key in ('bits', 'min_voltage', 'max_voltage'))
    if type(bits) is not int or not 1 <= bits <= NATIVE_BITS:
        raise ValueError('Invalid effective analog bit depth')
    if (not finite_number(low) or not finite_number(high) or not low < high
            or abs(low) > 1000 or abs(high) > 1000):
        raise ValueError('Invalid analog voltage range')
    maximum = (1 << bits) - 1
    return dict(bits=bits, min_voltage=low, max_voltage=high,
                effective_code_min=0, effective_code_max=maximum,
                voltage_step_v=(high - low) / maximum,
                native_transport_bits=NATIVE_BITS,
                effective_code_rule='native_code >> (16 - bits)')


def analog_metadata(value):
    if not isinstance(value, dict) or value.get('mode') not in ('normal', 'test'):
        raise ValueError('Missing or invalid analog API metadata')
    if type(value.get('saved')) is not bool or not isinstance(value.get('storage_error'), str):
        raise ValueError('Missing analog persistence status')
    return dict(dac=channel_metadata(value.get('dac')), adc=channel_metadata(value.get('adc')),
                saved=value['saved'], mode=value['mode'], storage_error=value['storage_error'],
                voltage_conversion_valid=value['saved'] and value['storage_error'] == 'ESP_OK',
                source='/analog/config', calibration_basis='firmware-configured endpoint labels')


def convert_sample(raw, channel, *, calibrated):
    if type(raw) is not int or not 0 <= raw < 1 << NATIVE_BITS:
        raise ValueError('Invalid native analog code')
    code = raw >> (NATIVE_BITS - channel['bits'])
    voltage = (channel['min_voltage'] + code * channel['voltage_step_v']) if calibrated else None
    return dict(native_code=raw, effective_code=code, voltage_v=voltage,
                voltage_valid=calibrated)


class Esp32Servo:
    capabilities = ('dac_monitor', 'adc_monitor', 'servo_status', 'analog_metadata')

    def __init__(self, host, port, device_id):
        self.host, self.port, self.device_id = host, port, device_id
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def close(self):
        pass  # Requests own their bounded HTTP connections.

    def set_control(self, values):
        """Owner-private finite allowlist; callers must prove the output envelope."""
        allowed = {'valid_data_index', 'target', 'system_min', 'system_max',
                   'control_offset', 'pid_kp', 'pid_ki', 'pid_kd',
                   'pid_integral_min', 'pid_integral_max', 'pid_reset_state',
                   'pid_multiplier', 'pid_max_interval', 'udp_enable', 'udp_port'}
        allowed.add('polling_interval')
        # Owner-private feedforward group (atomic in firmware 2026.10.21-antiwindup) and the
        # ADC validity window / target time matching the owner sets around it.
        allowed |= {'multiplier_control', 'baseline_control', 'multiplier_target', 'baseline_target',
                    'mod_valid_min', 'mod_valid_max', 'target_time_match'}
        allowed.add('log_adc_change_threshold')   # telemetry-only ADC change-row threshold (adc_log_threshold.py, 2026-10-23)
        if (not isinstance(values, dict) or not values or set(values) - allowed
                or any(not finite_number(v) for v in values.values())):
            raise ValueError('Only typed finite native control settings are supported')
        return self._send(values)

    def set_feedforward(self, values, *, expected_revision, profile_digest):
        """One atomic cycle-boundary FF transaction; never generic config writes."""
        from ldcon.owners.native_ff_proof import FF_KEYS
        if (not isinstance(values, dict) or set(values) != set(FF_KEYS)
                or any(not finite_number(v) for v in values.values())
                or type(expected_revision) is not int or not 0 <= expected_revision < 2**32-1
                or not isinstance(profile_digest, str) or len(profile_digest) != 64
                or any(c not in '0123456789abcdef' for c in profile_digest)):
            raise ValueError('Complete finite atomic feedforward transaction required')
        if not 0 <= values['mod_valid_min'] <= values['mod_valid_max'] <= 65535:
            raise ValueError('Invalid feedforward ADC window')
        reply = self._send({'feedforward': dict(values, expected_revision=expected_revision,
                                                profile_digest=profile_digest)})
        transaction = reply.get('feedforward', {})
        if (transaction.get('state') != 'committed' or transaction.get('error') != ''
                or transaction.get('config_revision') != expected_revision+1
                or reply.get('ff_config_revision') != expected_revision+1
                or reply.get('ff_profile_digest') != profile_digest):
            raise ValueError('Atomic feedforward acknowledgment is unconfirmed')
        return reply

    def _send(self, values):
        import websocket
        if not callable(getattr(websocket, 'create_connection', None)):
            raise ValueError('Native control requires the websocket-client package')
        ws = websocket.create_connection(f'ws://{self.host}:{self.port}/ws', timeout=1.5,
                                         http_no_proxy=[self.host])
        try:
            ws.send(json.dumps(values, allow_nan=False))
            raw = ws.recv()
            if len(raw) > 65536:
                raise ValueError('Native control reply is too large')
            reply = json.loads(raw)
            if not isinstance(reply, dict) or 'error' in reply:
                raise ValueError('Native control rejected: ' + str(reply))
            return reply
        finally:
            ws.close()

    def dac_limits(self):
        """Firmware DAC range limits (antiwindup); None when the firmware has no such API."""
        try:
            value = self.get('/dac/limits')
        except Exception:
            return None
        return value if isinstance(value, dict) and type(value.get('min_code')) is int and type(value.get('max_code')) is int else None

    def get(self, path):
        if path not in ('/analog/config', '/config', '/status', '/dac/limits'):
            raise ValueError('Device read path is not allowed')
        with self.http.open(f'http://{self.host}:{self.port}{path}', timeout=1.5) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Device JSON response too large')
        def reject_constant(value):
            raise ValueError(f'Invalid JSON numeric constant: {value}')
        result = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(result, dict):
            raise ValueError('Device JSON response must be an object')
        return result

    def read(self):
        started = time.time()
        metadata = analog_metadata(self.get('/analog/config'))
        config = self.get('/config')
        status = self.get('/status')
        if config.get('ff_contract_version') == 1:
            # Generic reads can straddle a cycle-boundary commit. Never build
            # a purported coherent proof sample from two configuration epochs.
            after_config = self.get('/config')
            if (config != after_config or
                    status.get('feedforward', {}).get('config_revision') != config.get('ff_config_revision')):
                raise ValueError('Feedforward configuration changed during sampling')
        # Never combine codes with ranges changed during this read sequence.
        if metadata != analog_metadata(self.get('/analog/config')):
            raise ValueError('Analog metadata changed during sampling')
        if not isinstance(status.get('firmware_version'), str):
            raise ValueError('Missing firmware identity')
        software = config.get('software_modulation_enable')
        if software not in (0, 1) or type(software) is bool:
            raise ValueError('Missing or invalid modulation source')
        # In normal mode mod_raw may be validity-held by firmware; in test
        # mode it is median ADC data. Do not claim independent ADC timestamps.
        valid = metadata['voltage_conversion_valid']
        adc = convert_sample(status.get('mod_raw'), metadata['adc'], calibrated=valid)
        adc.update(source='software_modulation' if software and metadata['mode'] == 'normal'
                   else 'status.mod_raw',
                   physical_input=not (software and metadata['mode'] == 'normal'),
                   may_be_validity_held=metadata['mode'] == 'normal' and not software)
        if config.get('ff_contract_version') == 1:
            snapshot = status.get('feedforward', {})
            raw = convert_sample(snapshot.get('adc_raw'), metadata['adc'], calibrated=valid)
            adc.update(raw_native_code=raw['native_code'], used_native_code=snapshot.get('adc_used'),
                       sample_valid=snapshot.get('mod_valid'), has_valid_sample=snapshot.get('mod_has_valid'),
                       invalid_count=snapshot.get('mod_invalid'), cycle_seq=snapshot.get('cycle_seq'),
                       cycle_time_us=snapshot.get('cycle_time_us'))
        dac = convert_sample(status.get('dac_val'), metadata['dac'], calibrated=valid)
        dac['source'] = 'status.dac_val (commanded code; not independent voltage readback)'
        limits = self.dac_limits() if 'dac_limits_http_v1' in (status.get('capabilities') or []) else None
        return dict(host=self.host, port=self.port, firmware_version=status['firmware_version'],
                    read_started_unix_s=started, read_finished_unix_s=time.time(),
                    acquisition_time_known=False, metadata=metadata, dac_limits=limits,
                    dac=dac, adc=adc, servo_config=config, servo_status=status)
