"""ECDL system projections and the physical writer boundary.

The connection visible to the controller issues exact permits. The physical
connection holds the transport, validates projected and intrinsic limits, and
accepts no unpermitted mutation. It never owns an editable laser definition.
"""
import base64
import copy
import inspect
import math
import re
import uuid

from experiment_core.configuration import Parameter
from experiment_core.errors import AuthorizationRequired
from experiment_core.records import digest

from ldcon.core.contracts import DeviceError
from ldcon.core.device_conditions import finite
from ldcon.owners.owner_projection import PhysicalProjectionGuard


def classify_policy(before, after):
    from ldcon.core.agent_authority import describe_changes, changes as changed_paths, window_quality_path, FREE, FORBIDDEN
    changes = describe_changes('lasers', before, after)
    if any(row['class'] == FORBIDDEN for row in changes):
        return Parameter('calibration', '', '', 'cycle')
    if any(row['class'] != FREE for row in changes):
        return Parameter('safety', '', '', 'cycle')
    quality = ('.control_settings.optical.single_mode_max',
               '.control_settings.optical.warning_max',  # deletion only, enforced by describe_changes
               '.control_settings.optical.classification_enabled')
    category = 'quality' if changes and all(
        window_quality_path(path) or any(key in '.'.join(path) for key in quality)
        for path in changed_paths(before, after)) else 'operational'
    return Parameter(category, '', '', 'cycle')


def encoded(value):
    """Finite, exact JSON representation, including complete binary payloads."""
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'data': base64.b64encode(value).decode('ascii')}
    if hasattr(value, 'dtype') and hasattr(value, 'tobytes'):
        return {'encoding': 'array', 'dtype': str(value.dtype), 'shape': list(value.shape),
                'data': base64.b64encode(value.tobytes()).decode('ascii')}
    if isinstance(value, dict):
        return {str(key): encoded(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [encoded(item) for item in value]
    if hasattr(value, 'item'):
        return encoded(value.item())
    return value


def operation(method, args=(), kwargs=None):
    return dict(method=method, args=encoded(list(args)), kwargs=encoded(kwargs or {}))


def constraints(device, name):
    settings = device['control_settings']
    resources = device['connection']['resources']
    axis = {'piezo': 'piezo', 'current_awg': 'current', 'tec_awg': 'temperature'}[name]
    limits = copy.deepcopy(device['absolute_conditions']['axes'][axis])
    result = dict(kind=name, axis=axis, axis_limits=limits, channels={},
                  calibration=copy.deepcopy(settings['axes'][axis]), cleanup_operations=[])
    if name == 'piezo':
        result['cleanup_operations'] = [operation('set', ({'pid_multiplier': 0},))]
        result['steady'] = copy.deepcopy(settings.get('steady_lock', {}))
        result['sweep'] = copy.deepcopy(settings.get('locked_sweep', {}))
    else:
        channel = resources[name]['channel']
        result['channels'][str(channel)] = dict(bounds=limits['bounds'], axis=axis)
        if name == 'current_awg':
            sweep = settings.get('locked_sweep', {}).get('awg', {})
            if sweep:
                result['channels'][str(sweep.get('channel', 2))] = dict(
                    bounds=copy.deepcopy(sweep['ch2_bounds_v']), max_vpp=sweep['max_vpp'], axis='modulation')
                result['max_points'] = sweep['max_points']
                result['max_sample_rate_hz'] = sweep['max_sample_rate_hz']
            result['dither'] = copy.deepcopy(settings.get('steady_lock', {}).get('dither', {}))
    return result


class PhysicalConnection:
    """The sole transport holder; every public mutation requires a host permit."""
    MUTATIONS = frozenset(('set', 'offset', 'configure_dither', 'upload_arb', 'arm_burst',
        'set_burst_cycles', 'select_burst_arb', 'select_continuous_arb', 'set_dc',
        'burst_off', 'fire', 'fire_one', 'ack', '_send', 'write_raw'))
    READS = frozenset(('get', 'read', 'describe', 'query', 'burst_readback', 'wave_readback',
                       'require_write_support', 'close'))
    SERVO_FIELDS = frozenset(('target', 'control_offset', 'pid_multiplier', 'multiplier_control',
        'multiplier_target', 'baseline_control', 'baseline_target', 'software_modulation_enable',
        'valid_data_index', 'system_min', 'system_max', 'mod_valid_min', 'mod_valid_max',
        'target_time_match', 'target_delay_us', 'pid_kp', 'pid_ki', 'pid_kd',
        'pid_integral_min', 'pid_integral_max', 'pid_reset_state', 'polling_interval',
        'log_force_full_data_on_new_system', 'log_forced_full_data_min_interval',
        'log_adc_change_threshold', 'log_full_data_min_interval'))

    def __init__(self, raw, name, definition, authority, journal):
        self.__transport = raw
        self.name = name
        self._waveforms={}
        initial = constraints(definition, name)
        selected = ['servo'] if name == 'piezo' else ['CH' + key for key in initial['channels']]
        resource = definition['connection']['resources'][name]
        identity = 'ecdl-' + digest([resource.get('host', resource.get('owner')), name])[:24]
        physical_resources=['servo'] if name=='piezo' else ['CH1','CH2']
        self.guard = PhysicalProjectionGuard(identity, physical_resources,
            definition_revision=digest(resource), intrinsic_validator=self.validate, journal=journal)
        self.binding = self.guard.bind(authority, selected)
        self.profile = getattr(raw, 'profile', None)

    def read(self, method, *args, **kwargs):
        if method not in self.READS:
            raise AuthorizationRequired('Unregistered physical read method')
        if method=='get' and (len(args)!=1 or str(args[0]).strip('/') not in ('config','status','dac/limits','analog/config')):
            raise AuthorizationRequired('Only registered read-only firmware paths are allowed')
        # A query method must not become an alternate SCPI write entry point.
        if method == 'query' and (len(args) != 1 or not isinstance(args[0], str)
                or not re.fullmatch(r'(?:\*IDN|\*OPC|COUP|MODE|ROSC|C[12]:(?:BSWV|OUTP|BTWV|MDWV|SWWV|ARWV|SRATE))\?', args[0].strip(), re.I)):
            raise AuthorizationRequired('Only registered read-only SCPI queries are allowed')
        return getattr(self.__transport, method)(*args, **kwargs)

    def execute(self, permit, op, args, kwargs):
        if op != operation(op.get('method'), args, kwargs) or op['method'] not in self.MUTATIONS:
            raise AuthorizationRequired('Physical payload differs from its exact operation')
        if callable(getattr(self.__transport,'configure_projection',None)) and getattr(self.__transport,'projection',None) is None:
            raise AuthorizationRequired('Remote writes require an explicitly configured system projection')
        def writer():
            result=getattr(self.__transport, op['method'])(*args, **kwargs)
            self.record_result(op)
            return result
        execute = self.guard.execute_cleanup if getattr(permit, 'cleanup', False) else self.guard.execute
        return execute(permit, op, writer)

    def remote_projection(self, authority, projection):
        configure = getattr(self.__transport, 'configure_projection', None)
        if configure is not None:
            if not getattr(self.__transport,'spec',{}).get('projection'):
                return  # Missing trust configuration preserves read-only diagnostics.
            configure(authority, projection, resources=sorted(self.binding.resources))

    def _channel(self, channel, projected):
        if type(channel) is not int or str(channel) not in projected['channels']:
            raise AuthorizationRequired('Channel is outside the physical projection')
        return projected['channels'][str(channel)]

    def _envelope(self, channel, offset, amplitude, projected, *, dc=False):
        channel_policy = self._channel(channel, projected)
        offset, amplitude = finite(offset), finite(amplitude)
        low, high = channel_policy['bounds']
        if dc and amplitude==0 and channel_policy.get('rest_v') is not None and offset==finite(channel_policy['rest_v']):
            if not -10.<=offset<=10.:
                raise DeviceError('DC rest exceeds physical output limits')
            return
        if (amplitude < 0 or amplitude > min(20., channel_policy.get('max_vpp', 20.))
                or not max(-10., low) <= offset - amplitude / 2 <= offset + amplitude / 2 <= min(10., high)):
            raise DeviceError('AWG output envelope exceeds physical projection')

    def _servo(self, values, projected):
        if isinstance(values, dict) and set(values) == {'pi_handover'}:
            from ldcon.owners.pi_handover import validate_request
            low, high = projected['axis_limits']['bounds']
            return validate_request(values['pi_handover'], self.read('get', 'config'), low, high)
        if not isinstance(values, dict) or not values or set(values) - self.SERVO_FIELDS:
            raise AuthorizationRequired('Unregistered firmware mutation')
        for value in values.values():
            finite(value)
        if 'valid_data_index' in values:
            expected=projected.get('steady',{}).get('native_input',{}).get('valid_data_index')
            if expected is None or type(values['valid_data_index']) is not int or values['valid_data_index']!=expected:
                raise AuthorizationRequired('Firmware input selection differs from the commissioned projection')
        merged = dict(self.read('get', 'config'), **values)
        low, high = projected['axis_limits']['bounds']
        if not max(0., low) <= finite(merged['control_offset']) <= min(65535., high):
            raise DeviceError('DAC command exceeds physical projection')
        for key in ('pid_multiplier', 'software_modulation_enable', 'target_time_match',
                    'log_force_full_data_on_new_system', 'pid_reset_state'):
            if key in values and values[key] not in (0, 1):
                raise DeviceError('Firmware boolean outside intrinsic limits: ' + key)
        for key in ('baseline_control', 'baseline_target', 'mod_valid_min', 'mod_valid_max'):
            if key in values and not 0 <= values[key] <= 65535:
                raise DeviceError('Firmware ADC code outside intrinsic limits')
        if merged.get('mod_valid_min', 0) > merged.get('mod_valid_max', 65535):
            raise DeviceError('Firmware ADC interval is inverted')
        if finite(merged.get('pid_kd', 0)) != 0 or merged.get('software_modulation_enable', 0) != 0:
            raise DeviceError('Unprojected derivative or software modulation')
        if ('target_delay_us' in values and not 0 <= values['target_delay_us'] <= 100000
                or 'polling_interval' in values and not 1 <= values['polling_interval'] <= 1000000
                or 'log_forced_full_data_min_interval' in values and not 0 <= values['log_forced_full_data_min_interval'] <= 1000000):
            raise DeviceError('Firmware timing outside intrinsic limits')
        for key,maximum in (('log_adc_change_threshold',65535),('log_full_data_min_interval',1000000)):
            if key in values and not 0 <= values[key] <= maximum:
                raise DeviceError('Firmware telemetry setting outside intrinsic limits')
        # The writer independently checks the complete reachable DAC envelope.
        # Host step/slew constraints do not constrain autonomous native PI.
        if merged.get('pid_multiplier') or merged.get('multiplier_control'):
            enabled=bool(merged.get('pid_multiplier'))
            inputs = [finite(merged['system_min']), finite(merged['system_max'])] if enabled else [0.]
            integral = [finite(merged['pid_integral_min']), finite(merged['pid_integral_max'])] if enabled else [0.]
            if enabled and (inputs[0] <= 0 or inputs[0] > inputs[1] or integral[0] > integral[1]):
                raise DeviceError('Native PI interval is invalid')
            outputs = []
            for raw in (merged.get('mod_valid_min', 0), merged.get('mod_valid_max', 65535)):
                base = merged['control_offset'] + merged.get('multiplier_control', 0) * (raw - merged.get('baseline_control', 0))
                target = merged['target'] + merged.get('multiplier_target', 0) * (raw - merged.get('baseline_target', 0)) if enabled else 0.
                kp,ki=(merged['pid_kp'],merged['pid_ki']) if enabled else (0.,0.)
                outputs += [base + kp * (target - sample) + ki * memory
                            for sample in inputs for memory in integral]
            if min(outputs) < max(0., low) or max(outputs) > min(65535., high):
                raise DeviceError('Native PI envelope exceeds physical projection')

    def validate(self, op, projected):
        method, args, kwargs = op['method'], op['args'], op['kwargs']
        if method not in self.MUTATIONS:
            raise AuthorizationRequired('Unregistered physical mutation')
        if projected.get('writes_enabled') is False and op not in projected.get('cleanup_operations',[]):
            raise AuthorizationRequired('The projected laser output authority is disabled')
        if self.name == 'piezo':
            if method != 'set' or len(args) != 1 or kwargs:
                raise AuthorizationRequired('Servo requires a typed atomic setting group')
            return self._servo(args[0], projected)
        if method in ('ack', '_send', 'write_raw'):
            data = args[0]
            binary = None
            if method == 'write_raw':
                raw = base64.b64decode(data['data'], validate=True)
                header,marker,binary = raw.partition(b'WAVEDATA,')
                if not marker or len(binary)%2 or not 4 <= len(binary) <= 2*min(8000000,projected.get('max_points',32768)):
                    raise DeviceError('Raw ARB data exceeds intrinsic or projected limits')
                data = header.decode('ascii')
            if not isinstance(data, str) or '\n' in data or ';' in data:
                raise AuthorizationRequired('One registered SCPI instruction is required')
            match = re.fullmatch(r'C([12]):(BSWV|BTWV|ARWV|WVDT|SRATE|OUTP) (.+)', data.strip(), re.I)
            if not match:
                raise AuthorizationRequired('Unprojected raw SCPI instruction')
            channel, command, tail = int(match[1]), match[2].upper(), match[3]
            self._channel(channel, projected)
            if command=='OUTP':
                if tail == 'ON':
                    self._output_enable(channel, projected)
                elif tail != 'OFF':
                    raise AuthorizationRequired('Only exact output ON/OFF is registered')
                return
            tokens = tail.rstrip(',').split(',')
            if command=='BTWV' and tokens==['MTRIG']:
                return
            carrier = command=='BTWV' and tokens[0]=='CARR'
            if command == 'BTWV' and tokens[0] == 'CARR':
                tokens = tokens[1:]
            if len(tokens)%2:
                raise AuthorizationRequired('SCPI field pairs must be complete')
            fields = dict(zip(tokens[0::2], tokens[1::2]))
            if len(fields)*2 != len(tokens):
                raise AuthorizationRequired('Repeated SCPI fields are not allowed')
            if command in ('BSWV', 'WVDT') or carrier:
                if set(fields) - {'WVTP', 'FRQ', 'FREQ', 'AMP', 'AMPL', 'OFST', 'PHASE', 'PHSE', 'WVNM', 'LENGTH'}:
                    raise AuthorizationRequired('Unprojected SCPI waveform field')
                state = self.read('read', channel)
                def number(value):
                    return finite(float(str(value).removesuffix('V').removesuffix('HZ')))
                if 'WVTP' in fields and fields['WVTP'] not in ('DC','ARB','SINE'):
                    raise AuthorizationRequired('Unprojected waveform type')
                for key in ('FRQ','FREQ'):
                    if key in fields and not 0<number(fields[key])<=100000000:
                        raise DeviceError('AWG frequency outside intrinsic limits')
                for key in ('PHASE','PHSE'):
                    if key in fields and not 0<=number(fields[key])<=360:
                        raise DeviceError('AWG phase outside intrinsic limits')
                if command=='WVDT':
                    if binary is None or not {'WVNM','FREQ','AMPL','OFST'}<=fields.keys():
                        raise AuthorizationRequired('A complete binary ARB upload is required')
                    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',fields['WVNM']):
                        raise AuthorizationRequired('Invalid waveform name')
                    if 'LENGTH' in fields and (not fields['LENGTH'].isdigit() or int(fields['LENGTH'])!=len(binary)):
                        raise DeviceError('ARB length differs from exact payload')
                amp = 0 if fields.get('WVTP') == 'DC' else number(fields.get('AMP', fields.get('AMPL', state['amplitude_vpp'])))
                self._envelope(channel, number(fields.get('OFST', state['value'])), amp, projected,
                               dc=fields.get('WVTP',state['waveform'])=='DC')
            elif command == 'BTWV':
                if set(fields) - {'STATE', 'TRSR', 'GATE_NCYC', 'TIME', 'STPS', 'DLAY', 'EDGE', 'MTRIG'}:
                    raise AuthorizationRequired('Unprojected burst field')
                enums={'STATE':{'ON','OFF'},'TRSR':{'MAN','EXT'},'GATE_NCYC':{'NCYC'},'EDGE':{'RISE','FALL'}}
                for key,value in fields.items():
                    if key in enums and value not in enums[key]:
                        raise DeviceError('Invalid burst enum')
                    if key=='TIME' and (not value.isdigit() or not 1<=int(value)<=1000000):
                        raise DeviceError('Invalid burst cycle count')
                    if key in ('STPS','DLAY'):
                        numeric=finite(float(value))
                        if not 0<=numeric<=(360 if key=='STPS' else 1000000):
                            raise DeviceError('Invalid burst timing')
                if fields.get('STATE')=='OFF' and projected.get('_execution_cleanup'):
                    self._cleanup_burst(channel)
            elif command == 'ARWV':
                if set(fields) != {'NAME'} or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', fields['NAME']):
                    raise AuthorizationRequired('Invalid projected waveform selection')
                state=self.read('read',channel)
                from ldcon.drivers.ecdl import arb_name_matches
                known=self._waveforms.get((channel,fields['NAME']))
                if known is not None:
                    self._envelope(channel,known['offset_v'],known['amp_vpp'],projected)
                elif not arb_name_matches(state['arb_name'],fields['NAME']):
                    raise AuthorizationRequired('Raw selection of an unverified stored bank is forbidden; use a typed upload/select operation')
                else:self._envelope(channel,state['value'],state['amplitude_vpp'],projected)
            elif command=='SRATE':
                if fields=={'MODE':'DDS'}:
                    return
                if set(fields)!={'MODE','VALUE'} or fields['MODE']!='TARB':
                    raise AuthorizationRequired('Only a projected arbitrary sample rate is registered')
                rate=finite(float(fields['VALUE']))
                if not 0<rate<=min(100000000,projected.get('max_sample_rate_hz',100000000)):
                    raise DeviceError('Sample rate exceeds physical projection')
            return
        # Binding the actual driver signature makes positional and keyword forms
        # equivalent without interpreting a request-provided method signature.
        signature = inspect.signature(getattr(self.__transport, method))
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if method == 'fire':
            for channel in values['channels']:
                self._channel(channel, projected)
            return
        channel = values.get('channel')
        self._channel(channel, projected)
        if method == 'offset':
            state = self.read('read', channel)
            self._envelope(channel, values['value'], state['amplitude_vpp'], projected,dc=state['waveform']=='DC')
        elif method == 'set_dc':
            self._envelope(channel, values['offset_v'], 0, projected,dc=True)
        elif method in ('select_burst_arb', 'select_continuous_arb', 'upload_arb'):
            self._envelope(channel, values['offset_v'], values['amp_vpp'], projected)
            if not 0 < finite(values['frequency_hz']) <= 100000000:
                raise DeviceError('Invalid AWG frequency')
            if method == 'upload_arb':
                import numpy as np
                data = values['codes']
                if isinstance(data, dict) and data.get('encoding') == 'array':
                    codes = np.frombuffer(base64.b64decode(data['data'], validate=True), dtype=data['dtype'])
                else:
                    codes = np.asarray(data)
                if (codes.ndim != 1 or not 2 <= codes.size <= min(8000000, projected.get('max_points', 32768))
                        or not np.isfinite(codes).all() or (codes < -32768).any() or (codes > 32767).any()):
                    raise DeviceError('ARB codes exceed intrinsic or projected limits')
        elif method == 'configure_dither':
            from ldcon.drivers.ecdl import dither_payload
            dither_payload(channel, values['frequency'], values['vpp'], values['previous'], values['programming'],
                           paused=values.get('paused', False), keep_bank=values.get('keep_bank', False))
            self._envelope(channel, values['previous']['value'], values['previous']['amplitude_vpp'], projected)
        elif method == 'arm_burst' and values['source'] not in ('MAN', 'EXT'):
            raise DeviceError('Invalid burst source')
        elif method == 'burst_off' and projected.get('_execution_cleanup'):
            self._cleanup_burst(channel)
        elif method == 'set_burst_cycles' and (type(values['cycles']) is not int or not 1 <= values['cycles'] <= 1000000):
            raise DeviceError('Invalid burst cycle count')

    def _cleanup_burst(self,channel):
        state=self.read('read',channel)
        if state['waveform']!='DC' and state['burst']!='OFF':
            raise DeviceError('Cleanup requires settled DC before disarming an unknown ARB burst')

    def _output_enable(self, channel, projected):
        """Opt-in dedicated output, with fresh electrical and trigger proof."""
        if self._channel(channel, projected).get('allow_output_enable') is not True:
            raise AuthorizationRequired('Output enable is not in this system projection')
        state = self.read('read', channel)
        if (state.get('load') != 'HZ' or state.get('polarity') != 'NOR'
                or state.get('modulation') != 'OFF' or state.get('sweep') != 'OFF'):
            raise DeviceError('Output enable requires HZ, normal polarity and no modulation/sweep')
        waveform = state.get('waveform')
        if waveform == 'DC':
            if state.get('burst') != 'OFF':
                raise DeviceError('DC output enable requires burst OFF')
        elif waveform == 'ARB':
            from ldcon.drivers.siglent import fields, number
            burst = fields(self.read('query', f'C{channel}:BTWV?'))
            if (burst.get('STATE') != 'ON' or burst.get('TRSR') != 'EXT'
                    or burst.get('GATE_NCYC') != 'NCYC' or burst.get('EDGE') != 'RISE'
                    or number(burst.get('TIME', 'nan')) != 1):
                raise DeviceError('ARB output enable requires one externally triggered record')
            from ldcon.drivers.siglent import arb_name_matches
            if not any(ch == channel and arb_name_matches(state.get('arb_name'), name)
                       for ch, name in self._waveforms):
                raise AuthorizationRequired('ARB output enable requires an acknowledged upload')
        else:
            raise AuthorizationRequired('Output enable permits only DC or verified external ARB')
        self._envelope(channel, state['value'], state['amplitude_vpp'], projected, dc=waveform == 'DC')

    def record_result(self, op):
        """Remember only acknowledged uploads; an uncertain bank is never usable."""
        if op['method']=='write_raw':
            raw=base64.b64decode(op['args'][0]['data'],validate=True)
            header=raw.split(b'WAVEDATA,',1)[0].decode('ascii').rstrip(',')
            match=re.fullmatch(r'C([12]):WVDT (.+)',header)
            if match:
                values=match[2].split(',');fields=dict(zip(values[::2],values[1::2]))
                self._waveforms[(int(match[1]),fields['WVNM'])]=dict(
                    amp_vpp=float(fields['AMPL']),offset_v=float(fields['OFST']))
        elif op['method']=='upload_arb':
            bound=inspect.signature(getattr(self.__transport,'upload_arb')).bind(*op['args'],**op['kwargs'])
            values=bound.arguments
            self._waveforms[(values['channel'],values['name'])]=dict(
                amp_vpp=values['amp_vpp'],offset_v=values['offset_v'])


def physical_validator(transport, name):
    """Reuse the exact intrinsic/projection checks in a remote physical owner.

    ``transport`` is the physical owner's read/driver adapter, never a request
    object. Its get/read and typed method signatures are registered host code.
    """
    physical = object.__new__(PhysicalConnection)
    physical._PhysicalConnection__transport = transport
    physical.name = name
    physical._waveforms={}
    class Validator:
        def __call__(self,op,projected):return physical.validate(op,projected)
        def record_result(self,op):physical.record_result(op)
    return Validator()


class SystemConnection:
    """Trusted system adapter; it owns permits, never a raw transport."""
    def __init__(self, physical, authority, *, binding=None):
        self._physical, self._authority = physical, authority
        self._binding=physical.binding if binding is None else binding

    @property
    def profile(self):
        return self._physical.profile

    def install(self, device, *, writes_enabled=None):
        physical = self._physical
        projected = constraints(device, physical.name)
        projected['writes_enabled']=self._authority.snapshot()['allow_output'] if writes_enabled is None else writes_enabled
        self.install_constraints(projected)

    def install_constraints(self,projected, *, preserve_cleanup=True):
        physical=self._physical
        projected=copy.deepcopy(projected)
        if physical.name!='piezo' and {'CH'+str(key) for key in projected.get('channels',{})}!=set(self._binding.resources):
            raise AuthorizationRequired('Projection channel scope differs from the trusted binding')
        old = physical.guard.inspect(self._binding)
        # Previously admitted restoration recipes stay exact and bounded by the
        # new validator. Do not mint a restoration recipe after an unknown write.
        previous = (old.get('projection') or {}).get('constraints', {}).get('cleanup_operations', []) if preserve_cleanup else []
        for recipe in previous:
            if recipe not in projected['cleanup_operations']:
                try:
                    physical.validate(recipe, projected)
                except (ValueError, DeviceError, AuthorizationRequired):
                    continue
                projected['cleanup_operations'].append(recipe)
        projection = self._authority.project(self._binding, projected)
        physical.guard.install(self._binding, projection, expected_revision=old['projection_revision'])
        physical.remote_projection(self._authority, projected)

    def _admit_restore(self, op):
        physical = self._physical
        state = physical.guard.inspect(self._binding)
        if state['fault']:
            return
        projected = copy.deepcopy(state['projection']['constraints'])
        recipes=[]
        if physical.name=='piezo' and op['method']=='set':
            before = physical.read('get', 'config')
            # Only reversible telemetry and commissioned sweep settings have a
            # restoration recipe. Target ramps and PI state are never replayed.
            restorable={'baseline_target','multiplier_target','baseline_control','multiplier_control',
                        'system_min','system_max','target_time_match','target_delay_us',
                        'mod_valid_min','mod_valid_max','log_adc_change_threshold','log_full_data_min_interval'}
            values = {key: before[key] for key in op['args'][0] if key in before and key in restorable}
            recipes = [operation('set', (values,))] if values else []
            recipes += [operation('set', ({key: value},)) for key, value in values.items()]
        elif physical.name!='piezo' and op['method']!='offset':
            from ldcon.drivers.ecdl import SDG_PROFILE
            for channel in projected['channels']:
                channel=int(channel)
                before=physical.read('read',channel)
                if before['burst']=='OFF':recipes.append(operation('burst_off',(channel,)))
                # SDG waveform-type transitions can glitch; its non-DC laser
                # channel has no DC cleanup recipe. Other channels retain the
                # pre-admitted, bounded DC-first restoration.
                if before['waveform']=='DC' or physical.profile!=SDG_PROFILE:
                    recipes.append(operation('set_dc',(channel,before['value'])))
        changed = False
        for recipe in recipes:
            if recipe not in projected['cleanup_operations']:
                physical.validate(recipe, projected)
                projected['cleanup_operations'].append(recipe)
                changed = True
        if changed:
            # Restoration admission is a bounded journal, not an ever-growing
            # history of output setpoints. Keep the invariant emergency recipe
            # and the most recent exact, validated restoration groups.
            emergency=[recipe for recipe in projected['cleanup_operations']
                       if recipe==operation('set',({'pid_multiplier':0},))]
            recent=[recipe for recipe in projected['cleanup_operations'] if recipe not in emergency]
            projected['cleanup_operations']=emergency+recent[-256:]
            projection = self._authority.project(self._binding, projected)
            physical.guard.install(self._binding, projection, expected_revision=state['projection_revision'])
            physical.remote_projection(self._authority, projected)

    def __getattr__(self, name):
        physical = self._physical
        if name in physical.READS:
            return lambda *args, **kwargs: physical.read(name, *args, **kwargs)
        if name not in physical.MUTATIONS:
            raise AttributeError(name)
        def write(*args, **kwargs):
            op = operation(name, args, kwargs)
            self._admit_restore(op)
            state = physical.guard.inspect(self._binding)
            issue = self._authority.permit_cleanup if state['fault'] else self._authority.permit
            permit = issue(self._binding, op, request_id=uuid.uuid4().hex)
            return physical.execute(permit, op, args, kwargs)
        return write


def attach(owner, hardware):
    """Transfer transports to physical writers before exposing hardware to control."""
    if getattr(hardware, '_projection_attached', False):
        return hardware
    connections = getattr(hardware, 'connections', None)
    if connections is None:
        # Injected test hardware must expose the same physical boundary as the
        # real adapter; an opaque object with unrestricted writes is not safe.
        return attach_test_hardware(owner, hardware)
    for name, raw in list(connections.items()):
        physical = PhysicalConnection(raw, name, owner.device, owner.policy_authority, owner.policy_journal)
        connection = SystemConnection(physical, owner.policy_authority)
        state=physical.guard.inspect(physical.binding)
        if not state['fault']:
            connection.install(owner.device)
        elif not owner.fault:
            owner.fault='Physical writer requires readback reconciliation: '+state['fault']
            owner.persist_fault()
        connections[name] = connection
    hardware._projection_attached = True
    install_emission_inhibit(owner, hardware)
    return hardware


def reconcile_reviewed(owner, evidence):
    """Only called after the domain's explicit read-only dither review proof."""
    for connection in getattr(owner.hardware,'connections',{}).values():
        physical=connection._physical
        if physical.guard.inspect(connection._binding)['fault']:
            physical.guard.reconcile(connection._binding,lambda _:dict(
                settled=True,quiesced=True,domain_evidence=copy.deepcopy(evidence)))
            connection.install(owner.device)


def attach_test_hardware(owner, hardware):
    """Guard a minimal injected rig used by offline adapter tests."""
    projected = dict(axes=copy.deepcopy(owner.device['absolute_conditions']['axes']))
    def validate(op, limits):
        name, args = op['method'], op['args']
        if name == 'move':
            axis, value = args
        elif name == 'prepare':
            axis, value = 'piezo', args[0]
        else:
            raise AuthorizationRequired('Opaque hardware mutation is not projected')
        low, high = limits['axes'][axis]['bounds']
        if not low <= finite(value) <= high:
            raise DeviceError('Output exceeds physical projection')
    guard = PhysicalProjectionGuard('ecdl-test-' + owner.policy_authority.system_id, list(projected['axes']),
        definition_revision=digest(owner.device['connection']), intrinsic_validator=validate, journal=owner.policy_journal)
    binding = guard.bind(owner.policy_authority)
    guard.install(binding, owner.policy_authority.project(binding, projected), expected_revision=guard.inspect(binding)['projection_revision'])
    for method in ('move', 'prepare'):
        raw = getattr(hardware, method, None)
        if raw is None:
            continue
        def write(*args, _method=method, _raw=raw, **kwargs):
            op = operation(_method, args, kwargs)
            permit = owner.policy_authority.permit(binding, op, request_id=uuid.uuid4().hex)
            return guard.execute(permit, op, lambda: _raw(*args, **kwargs))
        setattr(hardware, method, write)
    hardware._projection_attached = True
    hardware._test_projection = (guard, binding)
    return hardware


def install(owner, candidate):
    if owner.hardware is None:
        return {}
    device = candidate['devices'][owner.device_key]
    result = {}
    for name, connection in getattr(owner.hardware, 'connections', {}).items():
        connection.install(device,writes_enabled=candidate['allow_output'])
        physical = connection._physical
        result[name] = physical.guard.inspect(physical.binding)['projection_revision']
    test = getattr(owner.hardware, '_test_projection', None)
    if test is not None:
        guard, binding = test
        old = guard.inspect(binding)
        guard.install(binding, owner.policy_authority.project(binding, dict(axes=device['absolute_conditions']['axes'])), expected_revision=old['projection_revision'])
        result['rig'] = guard.inspect(binding)['projection_revision']
    owner.hardware.device = copy.deepcopy(device)
    install_emission_inhibit(owner, owner.hardware)
    return result


def verify(owner, candidate):
    result={}
    if owner.hardware is not None:
        participants=[(name,connection._physical.guard,connection._physical.binding)
                      for name,connection in getattr(owner.hardware,'connections',{}).items()]
        test=getattr(owner.hardware,'_test_projection',None)
        if test is not None:participants.append(('rig',*test))
        for name,guard,binding in participants:
            state=guard.inspect(binding)
            projection=state.get('projection') or {}
            if (state['fault'] or projection.get('policy_revision')!=owner.policy_authority._revision(candidate)
                    or projection.get('dependencies')!=owner.policy_authority.dependencies):
                raise DeviceError('Physical projection adoption readback differs from the candidate')
            result[name]=state['projection_revision']
    return dict(settled=True,source_revision=owner.policy_authority._revision(candidate),projections=result)


def install_emission_inhibit(owner, hardware):
    """An OFF/restart never inherits an old ON-current cleanup capability."""
    emission=getattr(owner,'emission',None)
    if emission is None:
        return
    state=emission.store.status()
    if not state['inhibited'] or state['phase']=='quiescing':
        return
    for name,connection in getattr(hardware,'connections',{}).items():
        projected=constraints(owner.device,name)
        projected['writes_enabled']=False
        projected['cleanup_operations']=[]
        connection.install_constraints(projected,preserve_cleanup=False)
