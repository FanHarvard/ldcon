"""Host-configured, leased native current control for an independent ESP owner.

This module never configures a wavemeter, waveform generator or laser controller.
All native mutations are typed and verified; observation endpoints use the cache.
"""
import copy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from ldcon.core.contracts import DeviceError
from ldcon.drivers.esp32_servo import channel_metadata, convert_sample, finite_number
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.monitor_owner import CachedMonitor

C_NM = 299792458e9
PRESERVED = ('multiplier_control', 'baseline_control', 'multiplier_target',
             'baseline_target', 'mod_valid_min', 'mod_valid_max',
             'software_modulation_enable', 'polling_interval', 'target_time_match',
             'target_delay_us')
CONTROLLED = ('valid_data_index', 'target', 'system_min', 'system_max',
              'control_offset', 'pid_kp', 'pid_ki', 'pid_kd', 'pid_integral_min',
              'pid_integral_max', 'pid_multiplier', 'pid_max_interval', 'udp_enable', 'udp_port', 'polling_interval')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def number(value, name, *, positive=False):
    if not finite_number(value) or (positive and value <= 0):
        raise ValueError('Invalid ' + name)
    return float(value)


def bounds(value, name):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('Invalid ' + name)
    lo, hi = [number(x, name) for x in value]
    if lo >= hi:
        raise ValueError('Reversed or empty ' + name)
    return lo, hi


def validate_policy(policy):
    """There are intentionally no device, gain, sign or calibration defaults."""
    from ldcon.owners.native_modulation_limits import FIELD, limited
    p = copy.deepcopy(policy)
    required = {'revision', 'firmware_version', 'calibration', 'dac', 'adc',
                'current_bounds_a', 'activation_range_a', 'max_step_a',
                'max_slew_a_per_s', 'update_interval_s', 'native', 'preserved',
                'max_sample_age_s', 'missing_timeout_s', 'heartbeat_timeout_s',
                'optical', 'writes_enabled', 'cld_interlock', 'udp_gate'}
    if not isinstance(p, dict) or not required <= set(p) or set(p) - required - {'emission', FIELD}:
        raise ValueError('Complete explicit native-current policy is required')
    limited(p)  # Validate without inserting the default or changing legacy policy digests.
    if 'emission' in p:
        from ldcon.owners.emission import validate_policy as validate_emission
        validate_emission(p['emission'])
    if type(p['writes_enabled']) is not bool:
        raise ValueError('writes_enabled must be boolean')
    for key in ('revision', 'firmware_version'):
        if not isinstance(p[key], str) or not p[key]:
            raise ValueError('Missing ' + key)
    for key in ('dac', 'adc'):
        channel_metadata(p[key])
    c = p['calibration']
    if not isinstance(c, dict) or set(c) != {'bias_current_a', 'zero_voltage_v', 'current_a_per_v',
            'gain_relative_uncertainty', 'wavelength_nm_per_a', 'evidence', 'verified', 'response_verified'}:
        raise ValueError('DAC/current and wavelength/current calibration are required')
    for key in ('bias_current_a', 'zero_voltage_v', 'current_a_per_v', 'gain_relative_uncertainty'):
        number(c[key], key)
    if not 0 <= c['gain_relative_uncertainty'] < 1:
        raise ValueError('Calibration uncertainty must be in [0, 1)')
    if not c['current_a_per_v']:
        raise ValueError('Current modulation coefficient must be nonzero with known sign')
    if (type(c['verified']) is not bool or type(c['response_verified']) is not bool
            or not isinstance(c['evidence'], str) or not c['evidence']):
        raise ValueError('Calibration evidence and verification state are required')
    bounds(p['current_bounds_a'], 'current bounds')
    lo, hi = bounds(p['activation_range_a'], 'activation range')
    if not lo <= 0 <= hi:
        raise ValueError('Activation range must contain zero')
    for key in ('max_step_a', 'max_slew_a_per_s', 'update_interval_s', 'max_sample_age_s',
                'missing_timeout_s', 'heartbeat_timeout_s'):
        number(p[key], key, positive=True)
    if p['missing_timeout_s'] > 120 or p['heartbeat_timeout_s'] > 120:
        raise ValueError('Native missing-data and heartbeat budgets must be at most 120 seconds')
    if min(p['missing_timeout_s'], p['heartbeat_timeout_s']) < 3 * p['max_sample_age_s']:
        raise ValueError('Native supervision budgets must cover three fresh observations')
    expected = p['preserved']
    if not isinstance(expected, dict) or set(expected) != set(PRESERVED):
        raise ValueError('Exact external modulation and polling configuration is required')
    for key, value in expected.items():
        number(value, key)
    if expected['software_modulation_enable'] != 0 or expected['multiplier_target'] != 0:
        raise ValueError('This stationary card requires physical ADC and a fixed wavelength target')
    if not 0 <= expected['mod_valid_min'] <= expected['mod_valid_max'] <= 65535:
        raise ValueError('Invalid physical ADC validity envelope')
    if not .1 <= expected['polling_interval'] <= 100:
        raise ValueError('Native polling interval must be explicitly bounded in milliseconds')
    n = p['native']
    native_required = {'input', 'kp_count_per_nm', 'ki_count_per_nm_s',
                       'integral_limit_count', 'capture_hz', 'interval_bounds_s', 'evidence', 'arm_enabled'}
    if (not isinstance(n, dict) or set(n) - native_required - {'acceptance_hz', 'optimization_bounds',
            'target_ramp_step_hz','target_feedforward_scale','target_feedforward_scale_bounds','recentre_integral_fraction'}
            or native_required - set(n)):
        raise ValueError('Explicit native PI profile is required')
    if type(n['arm_enabled']) is not bool:
        raise ValueError('Native arm_enabled must be boolean')
    from ldcon.owners.ecdl_target_ff import study_policy
    study_policy(n)
    recentre_integral_fraction(n)
    for key in ('kp_count_per_nm', 'ki_count_per_nm_s'):
        number(n[key], key)
    for key in ('integral_limit_count', 'capture_hz'):
        number(n[key], key, positive=True)
    if number(n.get('acceptance_hz', n['capture_hz']), 'acceptance_hz', positive=True) < n['capture_hz']:
        raise ValueError('Native input acceptance must cover the arm capture range')
    if not isinstance(n['evidence'], str) or not n['evidence']:
        raise ValueError('Native response-design evidence is required')
    if n['arm_enabled']:
        if not c['response_verified']:
            raise ValueError('Native PI requires a measured response of this external-modulation path')
        slope = number(c['wavelength_nm_per_a'], 'measured wavelength/current response') * c['current_a_per_v']
        if n['kp_count_per_nm'] * slope >= 0 or n['ki_count_per_nm_s'] * slope >= 0:
            raise ValueError('Native gains must oppose the measured wavelength/DAC slope')
    if 'optimization_bounds' in n:
        limits = n['optimization_bounds']
        if not isinstance(limits, dict) or set(limits) != {'kp_abs_max', 'ki_abs_max'}:
            raise ValueError('Explicit independent optimization gain ceilings are required')
        for key, field in (('kp', 'kp_count_per_nm'), ('ki', 'ki_count_per_nm_s')):
            if number(limits[key+'_abs_max'], key+' optimization ceiling', positive=True) < abs(n[field]):
                raise ValueError('Optimization ceiling is below the commissioned initial gain')
    ilow, ihigh = bounds(n['interval_bounds_s'], 'native input interval')
    if ilow <= 0 or ihigh > p['max_sample_age_s']:
        raise ValueError('Native interval must be positive and fresh')
    route = n['input']
    if not isinstance(route, dict) or set(route) != {'valid_data_index', 'channel', 'cycle', 'src', 'udp_port'}:
        raise ValueError('Explicit native UDP route required')
    if any(type(route[k]) is not int for k in ('valid_data_index', 'channel', 'cycle', 'udp_port')):
        raise ValueError('Native UDP indices and port must be integers')
    if not (0 <= route['valid_data_index'] <= 63 and 1 <= route['channel'] <= 64
            and route['cycle'] >= 0 and 1 <= route['udp_port'] <= 65535
            and isinstance(route['src'], str) and route['src']):
        raise ValueError('Invalid native UDP route')
    if p['udp_gate'] is not None:
        from ldcon.owners.esp32_udp_gate import validate_config
        validate_config(p['udp_gate'], route)
    optical = p['optical']
    if (not isinstance(optical, dict) or set(optical) != {'endpoint', 'channel', 'cycle', 'expected_laser', 'trust_source_clock'}
            or optical['trust_source_clock'] is not True or optical['channel'] != route['channel']
            or optical['cycle'] != route['cycle']):
        raise ValueError('Independent optical route must match native channel/cycle and trust acquisition clock')
    interlock = p['cld_interlock']
    if (not isinstance(interlock, dict) or set(interlock) != {'endpoint', 'instrument', 'resource'}
            or not all(isinstance(v, str) and v for v in interlock.values())):
        raise ValueError('CLD bias owner endpoint, instrument and pinned resource are required')
    return p


def current_for_code(p, code):
    voltage = convert_sample(code, channel_metadata(p['dac']), calibrated=True)['voltage_v']
    c = p['calibration']
    return c['bias_current_a'] + c['current_a_per_v'] * (voltage - c['zero_voltage_v'])


def current_bounds_for_codes(p, codes):
    c = p['calibration']
    values = [c['bias_current_a'] + (current_for_code(p, code) - c['bias_current_a']) * factor
              for code in codes for factor in (1-c['gain_relative_uncertainty'], 1+c['gain_relative_uncertainty'])]
    return [min(values), max(values)]


DEFAULT_RECENTRE_INTEGRAL_FRACTION = .5


def recentre_integral_fraction(n):
    """Post-step integral recentre threshold (target-ff design, human decision 5, 2026-10-27): after a target ramp,
    an |integral| above this fraction of the raw limit starts a BACKGROUND bounded recenter_ramp to integral_neutral.
    0 disables it (report only); the default 0.5 applies when the native profile does not name it."""
    value = n.get('recentre_integral_fraction', DEFAULT_RECENTRE_INTEGRAL_FRACTION)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('recentre_integral_fraction must be in [0, 1]')
    return float(value)


def dac_headroom(p, config, hz_per_code, *, scale=1., dac_limits=None, activation_code=None):
    """Report (never a refusal) how far the firmware target may still move, from the LIVE policy / firmware limits.

    The reachable envelope at the present PI centre (``actual_envelope``: offset +- correction, ADC FF) is compared with
    the admitted code window: the DAC rails, the firmware ``dac_limits`` (saved range guard) and the activation-relative
    / absolute current windows (``codes_window``). Nothing here hard-codes a range (decision 6, 2026-10-27).
    Returns ``{up_hz, down_hz, up_codes, down_codes, window_codes, envelope_codes}`` or None when not computable."""
    try:
        from ldcon.owners.native_policy_safety import actual_envelope
        proof = actual_envelope(p, config)
        lo, hi = proof['code_bounds']
        window = codes_window(p, dac_limits, activation_code=activation_code)
        if window is None:
            return None
        up = max(0., window[1] - hi)
        down = max(0., lo - window[0])
        hz = abs(float(hz_per_code)) / max(float(scale), 1e-9)
        return dict(up_hz=up * hz, down_hz=down * hz, up_codes=up, down_codes=down,
                    window_codes=[window[0], window[1]], envelope_codes=[lo, hi])
    except (BrokerError, ValueError, KeyError, TypeError):
        return None


def codes_window(p, dac_limits=None, *, activation_a=None, activation_code=None):
    """The admitted native code window [low, high]: DAC rails, firmware dac_limits, absolute current bounds and (when
    the activation is known) the activation-relative current window, all from the live policy / firmware."""
    low, high = 0., 65535.
    if isinstance(dac_limits, dict):
        for key, pick in (('min_code', max), ('max_code', min)):
            value = dac_limits.get(key)
            if type(value) in (int, float) and math.isfinite(value):
                low, high = (pick(low, float(value)), high) if key == 'min_code' else (low, pick(high, float(value)))
    c = p['calibration']
    dac = channel_metadata(p['dac'])
    per_code_a = c['current_a_per_v'] * dac['voltage_step_v'] / (1 << (16 - dac['bits']))
    if not per_code_a:
        return None
    # code for a current: invert current_for_code (linear in the code)
    zero = current_for_code(p, 0)
    def code_for(current):
        return (current - zero) / per_code_a
    bounds = sorted(code_for(x) for x in p['current_bounds_a'])
    low, high = max(low, bounds[0]), min(high, bounds[1])
    if activation_a is not None:
        rel = sorted(code_for(activation_a + x) for x in p['activation_range_a'])
        low, high = max(low, rel[0]), min(high, rel[1])
    elif activation_code is not None:
        rel = sorted(activation_code + x / per_code_a for x in p['activation_range_a'])
        low, high = max(low, rel[0]), min(high, rel[1])
    if low > high:
        return None
    return [low, high]


def acceptance_bounds(n, frequency_hz):
    hz = number(frequency_hz, 'target frequency', positive=True)
    width = number(n.get('acceptance_hz', n['capture_hz']), 'acceptance_hz', positive=True)
    if hz <= width:
        raise ValueError('Input acceptance interval contains nonphysical frequency')
    return [C_NM / (hz + width), C_NM / (hz - width)]


def envelope(p, target_nm, offset, *, open_loop=False):
    """Bound every firmware PI value, including stale ADC and saturated raw I.

    Human decision 2026-10-21 (sr1092): firmware-originated PI(+FF) output is
    not bound by a per-step/slew cap; only the DAC rails, the absolute current
    bounds and the activation-relative window (``_check_envelope``) apply.
    Host-written offset moves use the separately approved external-modulation
    step/slew policy. The autonomous reachable jump is reported, not enforced.
    """
    n, m = p['native'], p['preserved']
    hz = C_NM / number(target_nm, 'target wavelength', positive=True)
    # Every accepted value can affect P. A wider receiver range must widen
    # the output proof even when the narrower arm-entry capture is unchanged.
    wavelengths = acceptance_bounds(n, hz)
    error = max(abs(x - target_nm) for x in wavelengths)
    correction = 0. if open_loop else abs(n['kp_count_per_nm']) * error + n['integral_limit_count']
    ff = sorted(m['multiplier_control'] * (x - m['baseline_control'])
                for x in (m['mod_valid_min'], m['mod_valid_max']))
    low, high = offset + ff[0] - correction, offset + ff[1] + correction
    if not 0 <= low <= high <= 65535:
        raise ValueError('Native correction could reach a physical DAC rail')
    # Include one code for truncation/effective-bit quantization in each bound.
    codes = [max(0, math.floor(low)), min(65535, math.ceil(high))]
    currents = current_bounds_for_codes(p, codes)
    absolute = p['current_bounds_a']
    if not absolute[0] <= currents[0] <= currents[1] <= absolute[1]:
        raise ValueError('Full native PI + ADC envelope violates absolute current bounds')
    # The coefficient uncertainty is shared by both endpoints. Subtracting
    # independently expanded absolute bounds would count a static calibration
    # offset as an instantaneous current jump away from DAC zero.
    delta = abs(current_for_code(p, codes[1])-current_for_code(p, codes[0])) * (1+p['calibration']['gain_relative_uncertainty'])
    from ldcon.owners.native_modulation_limits import report
    return dict(code_bounds=codes, current_bounds_a=currents, maximum_jump_a=delta, correction_count=correction,
                system_bounds_nm=wavelengths, native_continuous_slew_limited=False,
                firmware_step_cap='none; host offset policy is reported separately',
                control_period_s=m['polling_interval'] * .001, **report(p))


def _same_setting(actual, expected):
    """Preserved/FF firmware settings: integers and flags exact; floats within the firmware's float32 precision
    (rel 1e-6, abs 1e-9 -- the same tolerance as the FF write readback). A real foreign change (e.g. 1 %) still differs."""
    numbers = (int, float)
    if (type(actual) in numbers and type(expected) in numbers and type(actual) is not bool and type(expected) is not bool
            and (type(actual) is float or type(expected) is float)):
        return math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-9)
    return actual == expected


SEQ_REGRESSED = 'Native sequence regressed; sample refused, baseline reset'


class NativeCurrentOwner(CachedMonitor):
    def __init__(self, driver, interval_s, max_age_s, status_file, policy, state_file,
                 *, clock=time.monotonic, optical=None, bias=None, gate=None, ff_profile=None,
                 assembly_factory=None, assembly_definition_revision=None, laser=None, cld_link=None):
        # Rule M step 4 (2026-10-27): ``policy`` / ``ff_profile`` are the laser yaml's (layer 2, requested at start);
        # the authority journal and the state file hold the EFFECTIVE policy and profile. The owner starts on the
        # effective ones and adopts the yaml at the end of __init__ when idle (see adopt_startup); under a lease the
        # yaml stays pending until a policy_update decision adopts it with the hardware re-check.
        validated = validate_policy(policy)
        from ldcon.owners.native_ff_proof import validate_profile
        requested_profile = validate_profile(ff_profile, validated) if ff_profile is not None else None
        previous = None
        state_path = Path(state_file)
        if state_path.exists():
            previous = json.loads(state_path.read_text())
        profile = requested_profile
        if previous is not None and 'ff_profile' in previous and previous['ff_profile'] != requested_profile:
            profile = previous['ff_profile']            # effective until adopted (idle start) or a policy_update
        from ldcon.owners.native_system_owner import NativeLaserSystemOwner, physical_definition
        from ldcon.owners.native_instrument_owner import NativeInstrumentOwner
        self.system_owner = NativeLaserSystemOwner(self, validated, profile, state_file,
                                                  revision_function=digest, assembly_factory=assembly_factory,
                                                  assembly_definition_revision=assembly_definition_revision,
                                                  laser=laser, cld_link=cld_link)
        self.requested_ff_profile = requested_profile
        self.ff_startup = dict(state='equal' if profile == requested_profile else 'pending', pending=profile != requested_profile)
        physical = NativeInstrumentOwner(driver, self.system_owner.authority, physical_definition(validated), journal=self.system_owner.journal)
        self.system_owner.instrument = physical
        super().__init__(physical, interval_s, max_age_s, status_file)
        self.ff = None
        self.ff_center_samples = []
        self.expected_preserved = dict(self.policy['preserved'])
        self.last_dac_write_failures = None
        self.dac_limits = None
        if interval_s > self.policy['max_sample_age_s']:
            raise ValueError('Owner poll interval exceeds native freshness budget')
        self.clock = clock
        self.io_lock = threading.RLock()
        self.state_file = Path(state_file)
        from ldcon.owners.adc_log_threshold import Journal as AdcLogJournal
        # 2026-10-23: temporary log_adc_change_threshold during an FF shot (telemetry only; journal survives a restart)
        self.adc_log = AdcLogJournal(self.state_file.with_name('adc-log-threshold.json'))
        from ldcon.owners.native_policy_reload import check_journal
        check_journal(self.state_file)
        from ldcon.owners.native_policy_update import check_journal as check_update_journal
        check_update_journal(self.state_file)
        self.lease = None
        self.pending = None
        self.fault = None
        self.active = False
        self.prepared = None
        self.input_configuration = None
        self.saved_pid = None
        self.pid_history = []
        self.pid_tuning = None
        from ldcon.owners.native_background import NativeBackground
        self.background = NativeBackground(self)
        self.retarget = None          # live target ramp state (rule F tune.lock_target, 2026-10-22)
        self.recentre = None          # live PI-centre ramp state (rule L tune.operating_point, 2026-10-27; not persisted)
        from ldcon.owners.ecdl_target_ff import TargetFFStudy
        self.target_ff_study = TargetFFStudy(self.clock)
        self.activation_a = None
        self.activation_measured_a = None
        self.activation_code = None
        self.activation_record = None   # claimed/armed times of the current lease (persisted for adoption)
        self.adoption = None            # restart adoption record of the current lease (pending/adopted/refused)
        self.shutdown_record = None     # last deliberate shutdown that left the PI running
        self.expected_config = None
        self.last_write = clock()
        self.last_seq = None
        self.last_seq_at = None
        self.last_optical = None
        self.qualified = 0
        self.last_good = None
        self.input_report, self.input_events = {}, []
        self.heartbeat = None
        self.input_error = 'No qualified optical acquisition'
        self.optical_sample = None
        self.bias_sample = None
        if bias is None:
            from ldcon.owners.esp32_native_client import BiasLease
            bias = BiasLease(self.policy['cld_interlock'])
        self.bias = bias
        if self.policy['udp_gate'] is None:
            gate = None  # Explicit direct mode never opens a UDP relay socket.
        elif gate is None:
            from ldcon.owners.esp32_udp_gate import UdpQualityGate
            gate = UdpQualityGate(self.policy['udp_gate'], self.policy['native']['input'], driver.host)
        self.gate = gate
        if optical is None:
            from ldcon.drivers.fizeau import HttpFizeauWavemeter
            c = self.policy['optical']
            optical = HttpFizeauWavemeter(c['endpoint'], channel=c['channel'], cycle=c['cycle'],
                                        expected_laser=c['expected_laser'], trust_source_clock=True,
                                        timeout_s=1.5, clock=clock)
        self.optical = optical
        if self.state_file.exists():
            old = json.loads(self.state_file.read_text())
            if old.get('definition') != self.definition:
                raise ValueError('Native owner saved policy differs; offline review required')
            self.lease, self.pending, self.fault = old['lease'], old['pending'], old['fault']
            self.bias.lease = old.get('bias_lease')
            self.input_configuration = old.get('input_configuration')
            self.saved_pid = old.get('saved_pid')
            self.pid_history = old.get('pid_history', [])
            self.ff = old.get('feedforward')
            background = old.get('background') or {}
            self.background.coordinator.generation = background.get('generation', 0)
            self.background.coordinator.reservations = set(background.get('reservations', []))
            self.background.coordinator.epoch = background.get('epoch')
            self.background.coordinator.last = background.get('last')
            if self.background.coordinator.epoch:
                self.background.coordinator.block('Owner restarted; checkpoint retained for reconciliation')
            if old.get('active') and self.lease and not self.pending and not self.fault:
                # Rule A/B, human decision 2026-10-26/27 (incident 2026-10-27 00:00): the previous owner left its
                # active PI running (deliberate shutdown, crash or kill). Adopt the same lease after a READ-ONLY
                # verification of the firmware and the CLD bias lease; latch only when something differs.
                self._restore_activation(old)
                self.adoption = dict(state='pending', started=self.clock(), started_unix_s=time.time(),
                                     lease=self.lease, attempts=0, bias_verified=None,
                                     previous_shutdown=old.get('shutdown'),
                                     previous_heartbeat_unix_s=old.get('heartbeat_unix_s'))
                if self.ff and self.ff.get('state') != 'restored':
                    self._refuse_adoption('a native feedforward session was in flight (state %s); FF values unverified'
                                          % self.ff.get('state'))
                else:
                    self._adopt()
            elif self.lease or self.pending or self.bias.lease or (self.ff and self.ff.get('state') != 'restored'):
                self.fault = self.fault or 'Previous owner retained a lease; offline review required'
        from ldcon.owners.native_current_tuning import effective_policy
        effective_policy(self.policy, self.saved_pid)  # Validate persisted reductions before use.
        self._persist()
        self.system_owner.sync_projection()
        self._adopt_startup()

    def _idle(self):
        """No lease, PI off, no intent/fault/FF session: a startup adoption changes no admitted trajectory."""
        return not (self.lease or self.active or self.pending or self.fault or self.bias.lease or self.prepared
                    or (self.ff and self.ff.get('state') != 'restored'))

    def _adopt_startup(self):
        """Rule M step 4: adopt the laser yaml (policy, FF profile, CLD conditions) at start when idle; report pending
        otherwise. The adoption is journaled with a startup receipt; nothing is written to the firmware."""
        migrated = self.system_owner.startup.get('migrated')
        if migrated:
            self._report_event('policy_dependencies_migrated', **migrated)
        idle = self._idle()
        try:
            state = self.system_owner.adopt_startup(idle=idle)
        except Exception as exc:
            raise ValueError('Startup adoption of the laser native policy failed: %s' % exc) from exc
        if state == 'adopted':
            self._report_event('policy_adopted_at_start', changed_paths=self.system_owner.startup['diff'],
                               policy_digest=self.definition)
        elif state == 'pending':
            self._report_event('policy_yaml_pending_at_start', changed_paths=self.system_owner.startup['diff'],
                               saved_revision=self.system_owner.startup['requested_revision'], effective=self.definition)
        if self.ff_startup['pending']:
            if idle:
                self.system_owner.set_ff_profile(self.requested_ff_profile)
                self.ff_startup.update(state='adopted', pending=False)
                self._report_event('ff_profile_adopted_at_start', ff_profile_digest=self.ff_definition)
            else:
                self._report_event('ff_profile_yaml_pending_at_start', effective=self.ff_definition,
                                   requested=digest(self.requested_ff_profile) if self.requested_ff_profile is not None else None)
        if self.system_owner.cld is not None:
            if self.system_owner.cld_startup['pending']:
                try:
                    state = self.system_owner.adopt_cld_startup(idle=idle)
                except Exception as exc:        # the CLD may be down: the yaml stays pending, retried by cld_tick
                    state = 'deferred'
                    self.system_owner.cld.last_error = str(exc)
                self._report_event('cld_policy_startup', state=state, changed_paths=self.system_owner.cld_startup['diff'])
            self.system_owner.cld_tick(self.clock(), force=True)
        self._persist()

    @property
    def policy(self):
        return self.system_owner.policy

    @property
    def definition(self):
        return self.system_owner.authority.revision

    @property
    def ff_profile(self):
        return copy.deepcopy(self.system_owner.ff_profile)

    @property
    def ff_definition(self):
        return digest(self.ff_profile) if self.ff_profile is not None else None

    def _unix_of(self, monotonic):
        """Wall-clock equivalent of one of this owner's monotonic stamps (for the durable state only)."""
        return None if monotonic is None else time.time() - (self.clock() - monotonic)

    def _activation_state(self):
        """Everything the next owner needs to adopt this lease's active PI without a write (2026-10-27)."""
        return dict(current_estimate_a=self.activation_a, measured_current_a=self.activation_measured_a,
                    dac_code=self.activation_code, dac_limits=copy.deepcopy(self.dac_limits),
                    record=copy.deepcopy(self.activation_record))

    def _restore_activation(self, old):
        activation = old.get('activation') or {}
        self.active = True
        self.activation_a = activation.get('current_estimate_a')
        self.activation_measured_a = activation.get('measured_current_a')
        self.activation_code = activation.get('dac_code')
        self.dac_limits = activation.get('dac_limits')
        self.activation_record = activation.get('record')
        self.expected_config = old.get('expected_config')
        self.prepared = old.get('prepared')
        self.retarget = old.get('retarget')
        self.shutdown_record = old.get('shutdown')

    def _refuse_adoption(self, reason):
        """The persisted active lease cannot be adopted: THIS is the real uncertain state. No firmware write."""
        self.adoption.update(state='refused', reason=str(reason), refused_unix_s=time.time())
        return self._latch('Owner restart adoption refused: ' + str(reason)
                           + '; native PI not touched, leases retained; offline review required')

    def _adopt(self):
        """Read-only verification of a persisted active lease: firmware identity/version, pid_multiplier == 1, the
        CONTROLLED configuration equal to the persisted expected_config, the activation-relative window (all in
        _identity) and the CLD bias lease (bias_status). On success the same lease resumes active with no fault; the
        worker gets a full heartbeat_timeout_s grace from this moment. A transport failure keeps the adoption pending
        (retried from poll) until missing_timeout_s; anything that differs latches with a precise reason."""
        a = self.adoption
        a['attempts'] += 1
        try:
            sample = self.driver.read()
        except (OSError, ValueError) as exc:
            a['last_error'] = str(exc)
            if self.clock() - a['started'] > self.policy['missing_timeout_s']:
                self._refuse_adoption('firmware state unreadable for %.0f s: %s' % (self.clock() - a['started'], exc))
            return False
        try:
            self._identity(sample)
        except (ValueError, TypeError, KeyError) as exc:
            self._refuse_adoption(str(exc))
            return False
        try:
            self._bias()
        except BrokerError as exc:
            if exc.code == 'fault':
                self._refuse_adoption(str(exc))
                return False
            a['bias_verified'] = False      # read gap only: the poll loop's missing_timeout_s applies as before
            a['bias_error'] = str(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            a['bias_verified'] = False
            a['bias_error'] = str(exc)
        else:
            a['bias_verified'] = True
        now = self.clock()
        self.heartbeat = self.last_good = self.last_write = now
        a.update(state='adopted', adopted_unix_s=time.time(), heartbeat_grace_s=self.policy['heartbeat_timeout_s'],
                 dac_code=sample['dac']['native_code'], pid_multiplier=sample['servo_config'].get('pid_multiplier'),
                 retarget_resumed=bool(self.retarget and self.retarget.get('active')))
        with self.lock:
            self.state.update(sample=sample, read_ok=True, state='connected',
                              last_success_unix_s=time.time(), error=None, failures=0)
        self._report_event('owner_restart_adopted', lease=self.lease, attempts=a['attempts'],
                           bias_verified=a['bias_verified'])
        return True

    def _persist(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + '.tmp')
        with tmp.open('w') as stream:
            json.dump(dict(schema_version=1, definition=self.definition, lease=self.lease,
                           bias_lease=self.bias.lease, pending=self.pending, fault=self.fault,
                           input_configuration=self.input_configuration, saved_pid=self.saved_pid,
                           pid_history=self.pid_history, feedforward=self.ff,
                           background=self.background.status(),
                           ff_profile_digest=self.ff_definition, ff_profile=self.ff_profile,
                           # 2026-10-27: the active PI survives an owner restart (adopted by the next owner)
                           active=self.active, activation=self._activation_state(),
                           expected_config=self.expected_config, prepared=self.prepared, retarget=self.retarget,
                           heartbeat_unix_s=self._unix_of(self.heartbeat), last_good_unix_s=self._unix_of(self.last_good),
                           adoption=self.adoption, shutdown=self.shutdown_record), stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.state_file)
        fd = os.open(self.state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _latch(self, reason):
        self.fault = str(reason)
        if self.ff and self.ff['state'] != 'restored':
            self.ff.update(state='fault', fault=self.fault)
        try:
            self._persist()
        except OSError as exc:
            self.fault += '; persistence failed: ' + str(exc)
        return BrokerError('fault', self.fault)

    def snapshot(self):
        result = super().snapshot()
        emission=self.system_owner.emission.dispatch({'action':'emission_status'})
        result['emission']=emission
        result['busy']=self.lease is not None or emission['phase'] in ('quiescing','executing','changing','verifying','unknown')
        result.update(read_only=not self.policy['writes_enabled'], fault=self.fault,
                      policy_digest=self.definition, native_pi_active=self.active,
                      input_valid=self.input_error is None, input_error=self.input_error,
                      qualified_samples=self.qualified, input_report=copy.deepcopy(self.input_report),
                      input_events=copy.deepcopy(self.input_events),
                      optical=copy.deepcopy(self.optical_sample),
                      bias=copy.deepcopy(self.bias_sample),
                      udp_gate=self.gate.snapshot() if self.gate is not None else None,
                      native_input_mode='gated' if self.gate is not None else 'direct',
                      preintegration_quality_gate=self.gate is not None,
                      input_configuration=copy.deepcopy(self.input_configuration),
                      pid_tuning=self.pid_tuning.state() if self.pid_tuning else None,
                      background=self.background.status(),
                      feedforward=copy.deepcopy(self.ff), ff_profile_digest=self.ff_definition,
                      leased=self.lease is not None, prepared=copy.deepcopy(self.prepared),
                      retarget=copy.deepcopy(self.retarget), recentre=copy.deepcopy(self.recentre),
                      adoption=copy.deepcopy(self.adoption), last_shutdown=copy.deepcopy(self.shutdown_record))
        from ldcon.owners.adc_log_threshold import public as adc_log_public
        result['adc_log_threshold'] = adc_log_public(self.adc_log.record)
        result['policy_authority'] = self.system_owner.policy_state()
        # target-ff design (2026-10-27): report-only room for the next target step and the PI integral usage.
        result['dac_headroom_hz'] = self._dac_headroom(result.get('sample'))
        result['integral_fraction'] = self._integral_fraction(result.get('sample'))
        result['firmware_contract'] = dict(dac_limits=copy.deepcopy(self.dac_limits),
                                           capabilities=list(((self.state.get('sample') or {}).get('servo_status') or {}).get('capabilities') or []),
                                           role='final output guard (saved DAC range limits); host supervision stays primary for PI/activation checks')
        result['capabilities'].append('native_current_lock')
        return result

    # ---- target-ff design (2026-10-27): report-only headroom / integral usage ---------------------------------------
    @staticmethod
    def _integral_fraction(sample):
        try:
            cfg, status = sample['servo_config'], sample['servo_status']
            limit = max(abs(float(cfg['pid_integral_min'])), abs(float(cfg['pid_integral_max'])))
            return abs(float(status['pid_integral'])) / limit if limit > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    def _dac_headroom(self, sample):
        """{up_hz, down_hz, ...} the firmware target may still move at the present PI centre (live policy, firmware
        dac_limits, activation window); None without a fresh sample or an active PI."""
        if not sample or not self.active or not self.prepared:
            return None
        try:
            settings = self.target_ff_study.settings(None, *self._target_ff_study_context())
            hz_per_code = self._hz_per_code(self.prepared['target_nm'])
        except (BrokerError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        return dac_headroom(self.policy, sample['servo_config'], hz_per_code, scale=settings['scale'],
                            dac_limits=self.dac_limits, activation_code=self.activation_code)

    def _post_retarget_recentre(self, state):
        """Hybrid integral recentre (human decision 5, 2026-10-27): once a target ramp has finished (completion is the
        frequency arrival the card judges), start ONE background bounded recenter_ramp of the PI centre to
        integral_neutral when |integral| exceeds recentre_integral_fraction. Never delays the completion report; a
        refusal is recorded on the retarget record, not raised."""
        r = self.retarget
        if not r or r.get('active') or r.get('recentre_check') != 'pending':
            return
        if not state.get('fresh') or self.input_error or self.pending:
            return
        sample = state['sample']
        fraction = self._integral_fraction(sample)
        threshold = recentre_integral_fraction(self.policy['native'])
        r['integral_fraction_after'] = fraction
        r['recentre_threshold'] = threshold
        r['dac_headroom_hz'] = self._dac_headroom(sample)
        r['recentre_check'] = 'done'
        if fraction is None or threshold <= 0 or fraction <= threshold:
            r['recentre'] = dict(status='not_needed', integral_fraction=fraction, threshold=threshold)
            self._report_event('retarget_recentre_skipped', integral_fraction=fraction, threshold=threshold)
            return
        try:
            cfg, status = sample['servo_config'], sample['servo_status']
            neutral = int(round(float(cfg['control_offset']) + float(status.get('pid_output', 0.) or 0.)))
            expected = int(cfg['control_offset'])
            if self.clock() - self.last_write < self.policy['update_interval_s']:
                r['recentre_check'] = 'pending'      # the ramp's last write is younger than the interval: next poll
                return
            started = self._recenter_ramp(dict(target_code=neutral, expected_code=expected,
                                               reason='post-retarget integral recentre (fraction %.2f > %.2f)' % (fraction, threshold)))
            r['recentre'] = dict(status='started', integral_fraction=fraction, threshold=threshold, target_code=neutral,
                                 start_code=expected, steps_planned=started.get('steps_planned'), started_unix_s=started.get('started_unix_s'))
            self._report_event('retarget_recentre_started', integral_fraction=fraction, threshold=threshold,
                               target_code=neutral, start_code=expected, steps_planned=started.get('steps_planned'))
        except (BrokerError, ValueError, KeyError, TypeError) as exc:
            r['recentre'] = dict(status='refused', integral_fraction=fraction, threshold=threshold, reason=str(exc))
            self._report_event('retarget_recentre_refused', integral_fraction=fraction, threshold=threshold, reason=str(exc))

    def _identity(self, sample):
        # see _same_setting: float settings tolerate the firmware's float32 storage, everything else is exact
        p, meta, status, cfg = self.policy, sample['metadata'], sample['servo_status'], sample['servo_config']
        if (sample['firmware_version'] != p['firmware_version'] or status.get('pid_time_based') is not True
                or meta['mode'] != 'normal' or not meta['voltage_conversion_valid']):
            raise ValueError('Firmware identity or normal saved analog configuration changed')
        for channel in ('adc', 'dac'):
            if any(meta[channel].get(k) != p[channel][k] for k in ('bits', 'min_voltage', 'max_voltage')):
                raise ValueError('Analog calibration changed: ' + channel)
        if any(not _same_setting(cfg.get(k), v) for k, v in self.expected_preserved.items() if k != 'polling_interval'):
            raise ValueError('External modulation or polling configuration changed')
        if status.get('step_test_state') not in ('idle', 'complete', 'aborted'):
            raise ValueError('A native step test is active or unknown')
        current = current_for_code(p, sample['dac']['native_code'])
        if not p['current_bounds_a'][0] <= current <= p['current_bounds_a'][1]:
            raise ValueError('Observed DAC current estimate violates absolute limits')
        if self.lease and self.dac_limits is not None:
            actual_limits = sample.get('dac_limits')
            if not actual_limits or any(actual_limits.get(k) != self.dac_limits.get(k) for k in ('min_code', 'max_code')):
                raise ValueError('Saved firmware DAC limits changed outside this lease')
        if self.activation_a is not None and not self.activation_a + p['activation_range_a'][0] <= current <= self.activation_a + p['activation_range_a'][1]:
            raise ValueError('Observed DAC violates activation-relative current limits')
        if self.active and cfg.get('pid_multiplier') != 1:
            raise ValueError('Native PI ownership lost')      # named before the generic CONTROLLED comparison
        if self.expected_config and any(not math.isclose(cfg.get(k, math.nan), v, rel_tol=1e-9, abs_tol=1e-9)
                                        for k, v in self.expected_config.items()):
            raise ValueError('Native controlled configuration changed outside this lease')
        from ldcon.owners.native_ff_proof import status_block
        ff_status = status_block(sample) if (self.ff_profile and self.ff_profile['enabled']) else status.get('feedforward', {})
        if (('dac_write_ok' in ff_status or (self.ff_profile and self.ff_profile['enabled']))
                and ff_status.get('dac_write_ok') is not True):
            raise ValueError('Native final DAC write is unconfirmed')
        if 'dac_write_ok' in ff_status:
            count = ff_status.get('dac_write_failures')
            if (ff_status.get('dac_output_unknown') is not False or type(count) is not int or count < 0
                    or (self.last_dac_write_failures is not None and count != self.last_dac_write_failures)):
                raise ValueError('Native DAC uncertainty or write-failure counter changed')
            self.last_dac_write_failures = count
        if self.ff and self.ff['state'] not in ('restored', 'fault') and self.ff.get('expected_revision') is not None:
            if (cfg.get('ff_config_revision') != self.ff['expected_revision']
                    or cfg.get('ff_profile_digest') != self.ff_definition
                    or cfg.get('dac_limit_enabled') is not False):
                raise ValueError('Native FF revision/profile/limiter changed outside this lease')

    def _bias(self):
        try:
            result = self.bias.status()
        except BrokerError as exc:
            if exc.code == 'measurement':
                raise
            raise BrokerError('fault', 'CLD bias ownership/fault response: ' + str(exc)) from exc
        self.bias_sample = result
        if result.get('fault') or result.get('bias_held') is not True:
            raise BrokerError('fault', 'CLD bias owner fault or ownership lost')
        if result.get('fresh') is not True:
            raise BrokerError('measurement', 'CLD bias cache is stale')
        state = result['sample']
        if not all(state.get(k) is True for k in ('ld_enabled', 'tec_enabled', 'modulation_enabled')):
            raise BrokerError('fault', 'CLD LD/TEC/modulation state is unsafe')
        if not math.isclose(state['current_a'], self.policy['calibration']['bias_current_a'], abs_tol=1e-9, rel_tol=0):
            raise BrokerError('fault', 'CLD bias differs from the current calibration')
        current = number(state['current_read_a'], 'CLD measured current')
        if not self.policy['current_bounds_a'][0] <= current <= self.policy['current_bounds_a'][1]:
            raise BrokerError('fault', 'CLD measured current violates absolute bounds')
        if self.activation_measured_a is not None:
            low, high = self.policy['activation_range_a']
            if not self.activation_measured_a + low <= current <= self.activation_measured_a + high:
                raise BrokerError('fault', 'CLD measured current violates activation-relative bounds')
        return state

    def _check_envelope(self, proof):
        from ldcon.owners.native_policy_safety import check_envelope
        return check_envelope(self, self.policy, proof)

    def _optical(self):
        """The host's OWN reading, for its own decisions (arm capture, open-loop offset, writes). Rule B, human decision
        2026-10-22: no quality/aligned filter on wavemeter data the firmware already consumes; only a finite frequency
        from the current cycle (age 0..max_sample_age_s; the receive time when the source clock is not known)."""
        optical = self.optical.read_frequency()
        self.optical_sample = asdict(optical)
        f = optical.frequency_hz
        if (f is None or not math.isfinite(f) or f <= 0
                or not 0 <= self.clock() - optical.acquired_s <= self.policy['max_sample_age_s']):
            raise ValueError('Independent optical measurement is stale or invalid')
        return optical

    def _input(self, sample):
        """Native input checks. KEPT: the UDP route/source configuration and sequence integrity (type, regression).
        REPORT-ONLY (human decision 2026-10-22, rule B): a stale sequence, the input interval, the independent reading
        and its difference from the native value. REMOVED: UDP quality/flags ("not valid, aligned": quality 512 =
        galvo confirmation pending, consumed by the firmware), the interval bounds, the native-vs-independent
        "disagree" comparison and the three-acquisition qualification."""
        p, status, cfg = self.policy, sample['servo_status'], sample['servo_config']
        route = p['native']['input']
        if cfg.get('valid_data_index') != route['valid_data_index'] or cfg.get('udp_enable') != 1 or cfg.get('udp_port') != route['udp_port']:
            raise ValueError('Native UDP route is not prepared')
        if any(status.get('last_udp_' + k) != route[k] for k in ('channel', 'cycle', 'src')):
            raise ValueError('Native UDP source/channel/cycle mismatch')
        if status.get('system_src') != 1:
            raise ValueError('Native input source is not UDP wavelength data')
        seq = status.get('last_udp_seq')
        if self.gate is not None and not self.gate.contains(seq, status.get('system_value')):
            raise ValueError('Native sample was not freshly accepted by this quality gate')
        if type(seq) is not int or seq < 0:
            raise ValueError('Native sequence is missing')
        now = self.clock()
        if self.last_seq is not None and seq < self.last_seq:
            # Report, refuse THIS sample for host decisions, reset the baseline (owner decision 2026-10-22): the
            # firmware itself rejects non-newer sequences (udp_input.cpp:215-238), so a host hold or stop afterwards
            # achieves nothing; the next poll is judged against the new baseline, so this never reaches the 120 s
            # deadline. A device restart is still caught by the firmware-level FF cycle check.
            self.input_report['native_sequence_regressions'] = self.input_report.get('native_sequence_regressions', 0) + 1
            self.input_report['native_sequence_last_regression'] = dict(time=now, previous=self.last_seq, seq=seq)
            self._report_event('native_sequence_regressed', previous=self.last_seq, seq=seq)
            self.last_seq, self.last_seq_at = seq, now
            raise ValueError(SEQ_REGRESSED)
        if self.last_seq is None or seq > self.last_seq:
            self.last_seq, self.last_seq_at = seq, now
        age = now - self.last_seq_at
        stale = age > p['max_sample_age_s']
        if stale != self.input_report.get('native_input_stale', False):
            self._report_event('native_input_stale' if stale else 'native_input_fresh', age_s=age, seq=seq)
        interval = status.get('last_system_interval_us')
        self.input_report.update(native_input_stale=stale, native_input_age_s=age, native_seq=seq,
                                 native_interval_s=interval * 1e-6 if isinstance(interval, (int, float)) else None,
                                 native_udp_quality=status.get('last_udp_quality'),
                                 native_udp_flags=status.get('last_udp_flags'))
        try:
            optical = self._optical()
        except (OSError, DeviceError, ValueError, KeyError, TypeError) as exc:
            self.input_report.update(optical_error=str(exc), native_optical_difference_hz=None)
            return None
        value = status.get('system_value')
        diff = (C_NM / value - optical.frequency_hz) if isinstance(value, (int, float)) and value > 0 else None
        self.input_report.update(optical_error=None, native_optical_difference_hz=diff)
        stamp = optical.acquired_unix_s if optical.acquired_unix_s is not None else optical.acquired_s
        if self.last_optical is None or stamp > self.last_optical:
            self.last_optical = stamp
            self.qualified += 1                  # report only (count of new independent readings)
        return optical

    def _report_event(self, kind, **values):
        """Report-only input event: a bounded in-memory list published in the status (no hold, no stop)."""
        self.input_events.append(dict(kind=kind, time=self.clock(), **values))
        del self.input_events[:-20]

    def _report_assembly_event(self, kind, values):
        """Keep step metadata separate from the event envelope's kind/time.

        Operating-point results legitimately contain their own ``kind``. They
        must not collide with this callback after a verified physical step.
        """
        self._report_event(kind, details=copy.deepcopy(values))

    def poll(self):
        return self.system_owner.poll()

    def _poll_system(self):
        with self.io_lock:
            if self.fault:
                return
            if self.adoption and self.adoption.get('state') == 'pending':
                # The persisted active lease is verified read-only before any supervision resumes.
                adopted = self._adopt()
                try:
                    self._persist()
                except OSError as exc:
                    self._latch('Adoption state persistence failed: ' + str(exc))
                    return
                if not adopted:
                    if not self.fault:
                        super().poll()      # status only; the firmware was not reachable
                    return
            bias_good = True
            if self.lease:
                try:
                    self._bias()
                except (OSError, BrokerError, ValueError, KeyError, TypeError) as exc:
                    if isinstance(exc, BrokerError) and exc.code == 'fault':
                        self._fatal(str(exc))
                        return
                    self.input_error = str(exc)
                    bias_good = False
            super().poll()
            state = super().snapshot()
            if state['fresh']:
                try:
                    self._identity(state['sample'])
                except (ValueError, TypeError, KeyError) as exc:
                    if self.lease:
                        self._fatal(str(exc))
                        return
                    self.input_error = str(exc)
                    return
                try:
                    if not self.active and self.prepared and self.prepared.get('mode') == 'acquisition':
                        self._optical()
                    else:
                        self._input(state['sample'])
                except (OSError, DeviceError, ValueError, TypeError, KeyError) as exc:
                    self.input_error = str(exc)
                    self.qualified = 0
                else:
                    self.input_error = None
                    if bias_good:
                        self.last_good = self.clock()
                    else:
                        self.input_error = 'CLD bias observation unavailable'
                        self.qualified = 0
            else:
                self.input_error = state.get('error', 'Native read unavailable')
                self.qualified = 0
            if self.active:
                now = self.clock()
                if self.heartbeat is None or now - self.heartbeat > self.policy['heartbeat_timeout_s']:
                    self._fatal('Native client heartbeat expired')
                elif self.last_good is None or now - self.last_good > self.policy['missing_timeout_s']:
                    self._fatal('Native continuous valid-input deadline exceeded')
            if not self.fault:
                self.background.poll()
            if self.system_owner.cld is not None and not self.fault:
                self.system_owner.cld_tick(self.clock())      # keeps the CLD projection installed; never fatal
            if self.active and self.retarget and self.retarget.get('active') and not self.fault:
                self._retarget_poll(state)
            if self.active and self.retarget and self.retarget.get('recentre_check') == 'pending' and not self.fault:
                self._post_retarget_recentre(state)
            if self.active and self.recentre and self.recentre.get('active') and not self.fault:
                self._recentre_poll(state)
            if self.ff_profile and not self.fault:
                try:
                    self._ff_poll(state)
                except (OSError, BrokerError) as exc:
                    if not self.fault:
                        self._fatal('FF supervision failed: '+str(exc))
            if not self.fault:
                self._adc_log_tick()

    def run(self):
        if self.gate is not None:
            self.gate.start()
        try:
            while not self.stop.is_set():
                self.poll()
                self.stop.wait(self.interval_s)
        finally:
            if not self.system_owner.emission.shutdown():
                return  # No transport/lease release during an unfinished physical effect.
            self.system_owner.assemblies.shutdown()
            # Deliberate shutdown (SIGTERM, systemctl restart) with an active PI: NO firmware write and NO fault
            # (rule A: a running lock does not depend on the owner; rule B: the firmware keeps the PI inside its
            # caps). The state file keeps active/lease/bias lease/expected_config/activation so the next owner adopts
            # the lock (__init__ -> _adopt). Uncertain state (fault) stays quarantined as before; nothing is replayed.
            # Before 2026-10-27 this block emergency-disabled the PI and latched "Independent native owner is shutting
            # down", which orphaned the sr1092 lock at the 2026-10-27 00:00 restart.
            with self.io_lock:
                if self.active and not self.fault:
                    self.shutdown_record = dict(
                        unix_s=time.time(), lease=self.lease, dac_code=(self.state.get('sample') or {}).get('dac', {}).get('native_code'),
                        heartbeat_age_s=None if self.heartbeat is None else self.clock() - self.heartbeat,
                        reason='deliberate owner shutdown; native PI left running for adoption by the next owner')
                    try:
                        self._persist()
                    except OSError as exc:
                        self._latch('Shutdown state persistence failed; adoption impossible: ' + str(exc))
            self.driver.close()
            if self.gate is not None:
                self.gate.close()

    def _read_before(self, *, require_input=False):
        if self.lease:
            try:
                self._bias()
            except BrokerError as exc:
                if exc.code == 'fault':
                    raise self._latch(exc) from exc
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise BrokerError('measurement', 'Prewrite CLD read unavailable; no mutation attempted') from exc
        try:
            sample = self.driver.read()
        except (OSError, ValueError) as exc:
            raise BrokerError('measurement', 'Prewrite native read unavailable; no mutation attempted: ' + str(exc)) from exc
        if 'read_started_unix_s' in sample and time.time()-sample['read_started_unix_s'] > self.policy['max_sample_age_s']:
            raise BrokerError('measurement', 'Prewrite native read exceeded freshness budget; no mutation attempted')
        try:
            self._identity(sample)
        except (ValueError, TypeError, KeyError) as exc:
            raise self._latch(exc) from exc
        if require_input:
            try:
                self._input(sample)
            except (OSError, DeviceError, ValueError, TypeError, KeyError) as exc:
                raise BrokerError('measurement', 'Prewrite native input unavailable; no mutation attempted: ' + str(exc)) from exc
            try:
                self._optical()          # the host's own finite current-cycle reading (no quality filter)
            except (OSError, DeviceError, ValueError, KeyError, TypeError) as exc:
                raise BrokerError('measurement', 'A finite current-cycle optical reading is required: ' + str(exc)) from exc
        return sample

    def _write(self, values, before, reason):
        from ldcon.owners.native_instrument_owner import NativePrewriteRefusal
        self.pending = dict(reason=reason, values=values)
        try:
            self._persist()
        except OSError as exc:
            raise self._latch('Write-intent persistence failed: ' + str(exc)) from exc
        try:
            self.driver.set_control(values)
            after = self.driver.read()
            expected = {k: before['servo_config'][k] for k in (*PRESERVED, *CONTROLLED)}
            expected.update({k: v for k, v in values.items() if k != 'pid_reset_state'})
            for key, value in expected.items():
                if not math.isclose(after['servo_config'].get(key, math.nan), value, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError('Native write verification failed: ' + key)
            prior = self.expected_config
            self.expected_config = {k: expected[k] for k in CONTROLLED}
            try:
                self._identity(after)
            except BaseException:
                self.expected_config = prior
                raise
            self.pending = None
            self.last_write = self.clock()
            self._persist()
            with self.lock:
                self.state.update(sample=after, read_ok=True, state='connected',
                                  last_success_unix_s=time.time(), error=None, failures=0)
            return after
        except NativePrewriteRefusal:
            self.pending = None
            self._persist()
            raise
        except Exception as exc:
            reason = 'Uncertain native write; lease retained: ' + str(exc)
            if values.get('pid_multiplier') == 1 or (self.active and values.get('pid_multiplier') != 0):
                # This is a distinct bounded emergency disable, not a retry of
                # the uncertain enable. Keep the original intent/fault/lease.
                try:
                    self.driver.emergency_disable()
                    disabled = self.driver.read()
                    if (disabled['servo_config']['pid_multiplier'] != 0
                            or disabled['servo_config']['control_offset'] != before['servo_config']['control_offset']):
                        raise ValueError('Fixed-reference emergency disable mismatch')
                    self.active = False
                    reason += '; emergency PI disable verified, review still required'
                except Exception as stop_error:
                    reason += '; emergency disable uncertain: ' + str(stop_error)
            raise self._latch(reason) from exc

    def _fatal(self, reason):
        # One new emergency disable at the proven fixed reference; never replay
        # an uncertain write or infer restoration from a lost reply.
        if self.active and not self.fault:
            try:
                before = self.driver.read()
                self.active = False
                self._write({'pid_multiplier': 0}, before, 'fatal fixed-reference disable')
            except Exception as exc:
                reason += '; disable could not be verified: ' + str(exc)
        self._latch(reason)

    def dispatch(self, request):
        return self.system_owner.dispatch(request)

    def _dispatch_system(self, request):
        if request.get('device_id') != self.driver.device_id:
            raise BrokerError('configuration', 'Native device identity mismatch')
        action = request.get('action')
        if action == 'status':
            return self.snapshot()
        with self.io_lock:
            # Revalidate after acquiring the effect mutex; an emission reservation
            # may have arrived while this request waited for the previous write.
            self.system_owner.emission.store.require_control(action)
            if action == 'policy_reload':
                from ldcon.owners.native_policy_reload import reload
                return reload(self, request)
            if action in ('policy_update', 'policy_update_prepare'):
                # Live, lease-preserving safety-class update (see native_policy_update).
                from ldcon.owners.native_policy_update import update
                return update(self, request)
            if action == 'policy_update_status':
                from ldcon.owners.native_policy_update import status
                return status(self, request)
            if action == 'describe':
                from ldcon.owners.native_current_tuning import effective_policy
                from ldcon.owners.native_policy_safety import gain_state, running_gain_policy, policy_layers
                effective = effective_policy(self.policy, self.saved_pid)
                if self.active and self.expected_config:
                    effective = running_gain_policy(self.policy, self.expected_config)
                return dict(device_id=self.driver.device_id, host=self.driver.host,
                            policy=copy.deepcopy(self.policy), policy_digest=self.definition,
                            ff_profile=copy.deepcopy(self.ff_profile), ff_profile_digest=self.ff_definition,
                            effective_policy=effective, gain_state=gain_state(self), policy_layers=policy_layers(self),
                            policy_authority=self.system_owner.policy_state(),
                            fault=self.fault, leased=self.lease is not None,
                            capabilities=['recenter', 'recenter_ramp', 'retarget', 'target_ff_study', 'emission'],
                            emission=self.system_owner.emission.dispatch({'action':'emission_status'}))
            if action == 'ff_status' and self.fault:
                if not self.lease or request.get('lease') != self.lease:
                    raise BrokerError('ownership', 'Exact native device lease required')
                return self._ff_dispatch(request)
            if self.fault:
                raise BrokerError('fault', self.fault)
            if action == 'claim':
                if self.lease:
                    raise BrokerError('busy', 'Native device already has a retained lease')
                if not self.policy['writes_enabled'] or not self.policy['calibration']['verified'] or request.get('policy_digest') != self.definition:
                    raise BrokerError('configuration', 'Verified calibration and matching enabled policy are required')
                before = self._read_before()
                if before['servo_config'].get('pid_multiplier') != 0:
                    raise BrokerError('busy', 'PI must already be disabled before claim')
                if self.system_owner.cld is not None:
                    # Rule M step 3: the CLD checks its bias bounds against this system's projection; install it first.
                    # If the projection cannot be installed (signing key absent, CLD without a source), the bias claim
                    # proceeds as before step 3 and the CLD decides from its own policy; this is reported, not fatal.
                    if not self.system_owner.cld_tick(self.clock(), force=True):
                        self._report_event('cld_projection_unavailable', error=str(self.system_owner.cld.last_error),
                                           note='bias claim proceeds on the CLD\'s own policy (pre-step-3 path)')
                try:
                    self.bias.claim(self.driver.device_id)
                    self._persist()
                except Exception as exc:
                    raise self._latch('CLD bias claim result requires review: ' + str(exc)) from exc
                try:
                    self._bias()
                    before = self._read_before()
                except BaseException:
                    self.bias.release(native_stopped=True)
                    raise
                if before['servo_config'].get('pid_multiplier') != 0:
                    raise self._latch('Native PI enabled during CLD bias claim; both leases retained')
                self.activation_a = current_for_code(self.policy, before['dac']['native_code'])
                self.activation_code = before['dac']['native_code']
                self.activation_measured_a = self.bias_sample['sample']['current_read_a']
                # Saved firmware DAC caps are immutable physical guards here.
                # Laser operating bounds independently constrain every admitted output.
                self.dac_limits = before.get('dac_limits')
                if 'dac_limits_http_v1' in (before['servo_status'].get('capabilities') or []):
                    if not self.dac_limits:
                        raise BrokerError('configuration', 'Firmware advertises DAC limits but none could be read')
                # Authorization windows and physical caps are different layers.
                # A cap need not cover every authorized command, or equal a newly
                # narrowed operating window: every reachable-output proof checks
                # their intersection. Never rewrite a physical cap in claim.
                from ldcon.owners.native_policy_safety import actual_envelope, check_envelope
                try:
                    check_envelope(self, self.policy, actual_envelope(self.policy, before['servo_config']), before)
                except (BrokerError, ValueError, TypeError, KeyError):
                    self.activation_a = self.activation_code = self.activation_measured_a = None
                    try:
                        self.bias.release(native_stopped=True)
                        self._persist()
                    except Exception as error:
                        raise self._latch('Rejected claim bias release is uncertain: ' + str(error)) from error
                    raise
                self.lease = uuid.uuid4().hex
                self.last_write = self.clock()
                self.activation_record = dict(claimed_unix_s=time.time(), armed_unix_s=None,
                                              client=str(request.get('client', ''))[:200] or None)
                self.adoption = self.shutdown_record = None
                try:
                    self._persist()
                except OSError as exc:
                    raise self._latch(exc) from exc
                return dict(lease=self.lease, activation_current_estimate_a=self.activation_a,
                            activation_measured_current_a=self.activation_measured_a,
                            activation_dac_code=self.activation_code)
            if not self.lease or request.get('lease') != self.lease:
                raise BrokerError('ownership', 'Exact native device lease required')
            if action == 'heartbeat':
                self.heartbeat = self.clock()
                return True
            if action in ('target_ff_study_status','target_ff_study_set','target_ff_study_restore'):
                return self._target_ff_study_dispatch(request)
            if isinstance(action, str) and action.startswith('background_'):
                return self.background.dispatch(request)
            if action in ('recenter','recenter_ramp','retarget','ff_begin','pid_begin','pid_target','pid_step','pid_save','pid_finish','offset','probe_offset'):
                self.background.boundary(action)
            if action in ('ff_validate', 'ff_begin', 'ff_status', 'ff_heartbeat', 'ff_end'):
                return self._ff_dispatch(request)
            if action == 'recenter_ramp_stop':
                return self._recenter_ramp_stop(str(request.get('reason', 'requested'))[:200])
            if self.ff and self.ff['state'] not in ('restored', 'fault') and action != 'stop' and not (
                    action == 'recenter' and self.ff['state'] == 'active' and self.ff.get('continuous')):
                # v5.8: the one exception is the bounded PI-centre following during an ACTIVE CONTINUOUS FF session
                # (human 2026-10-23: the slow following stays ON during the triangle); _recenter re-proves the FF.
                raise BrokerError('busy', 'Finish native feedforward before other mutations')
            if action == 'release':
                if self.active or self.pending:
                    raise BrokerError('busy', 'Verified native stop required before release')
                before = self._read_before()
                if before['servo_config']['pid_multiplier'] != 0:
                    raise self._latch('Cannot release an enabled native PI')
                old = self.lease
                self.bias.release(native_stopped=True)
                self.lease = None
                try:
                    self._persist()
                except OSError as exc:
                    self.lease = old
                    raise self._latch(exc) from exc
                self.activation_a = self.prepared = self.expected_config = None
                self.retarget = None
                self.recentre = None
                self.activation_code = self.activation_measured_a = None
                self.activation_record = self.adoption = self.shutdown_record = None
                try:
                    self._persist()
                except OSError as exc:
                    raise self._latch(exc) from exc
                return True
            if action == 'acquire_prepare':
                return self._acquire_prepare(request)
            if action == 'acquire_offset':
                return self._offset(request, acquisition=True)
            if action == 'prepare':
                return self._prepare(request)
            if action == 'configure_input':
                return self._configure_input(request)
            if action == 'offset':
                return self._offset(request)
            if action == 'probe_offset':
                return self._offset(request, probe=True)
            if action == 'recenter':
                return self._recenter(request)
            if action == 'recenter_ramp':
                return self._recenter_ramp(request)
            if action == 'retarget':
                return self._retarget(request)
            if action in ('pid_state', 'pid_check', 'pid_begin', 'pid_target', 'pid_step', 'pid_save', 'pid_finish'):
                if action == 'pid_begin':
                    from ldcon.owners.native_policy_safety import gain_state
                    if gain_state(self)['gains_pending']:
                        raise BrokerError('configuration', 'Requested PI gains are pending; apply them through prepare before starting a new gain comparison')
                from ldcon.owners.native_current_tuning import dispatch
                return dispatch(self, request)
            if action == 'arm':
                if not self.policy['native']['arm_enabled']:
                    raise BrokerError('configuration', 'Native arm permission is disabled')
                if self.prepared and self.prepared.get('mode') == 'acquisition':
                    raise BrokerError('request', 'PI-off acquisition does not authorize arm; prepare the full PI profile first')
                if self.active or not self.prepared:
                    raise BrokerError('request', 'Prepare an inactive native PI first')
                before = self._read_before(require_input=True)
                if abs(C_NM / self.prepared['target_nm'] - self.optical_sample['frequency_hz']) > self.policy['native']['capture_hz']:
                    raise BrokerError('measurement', 'Target is outside the qualified native capture interval')
                from ldcon.owners.native_policy_safety import actual_envelope
                enabling = dict(before['servo_config'], pid_multiplier=1)
                self._check_envelope(actual_envelope(self.policy, enabling))
                self._write({'pid_reset_state': 1}, before, 'reset native integral before enable')
                before = self._read_before(require_input=True)
                self._check_envelope(actual_envelope(self.policy, dict(before['servo_config'], pid_multiplier=1)))
                after = self._write({'pid_multiplier': 1}, before, 'enable native current PI')
                self.active = True
                self.heartbeat = self.last_good = self.clock()
                if self.activation_record is not None:
                    self.activation_record['armed_unix_s'] = time.time()
                try:
                    self._persist()      # active=True is durable (adoption after an owner restart)
                except OSError as exc:
                    raise self._latch('Activation persistence failed: ' + str(exc)) from exc
                return dict(armed=True, dac=after['dac'])
            if action == 'stop':
                if self.ff and self.ff['state'] not in ('restored', 'fault'):
                    self._ff_request_end('Native stop requested')
                    raise BrokerError('busy', 'Feedforward unwind must complete before native stop')
                if not self.active:
                    return dict(stopped=True, already_inactive=True)
                try:
                    before = self.driver.read()
                    self._identity(before)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise self._latch('Native stop read unavailable; lease retained: ' + str(exc)) from exc
                self.active = False
                after = self._write({'pid_multiplier': 0}, before, 'stop at qualified fixed reference')
                self.pid_tuning = None
                self.retarget = None
                self._recenter_ramp_stop('native stop')
                self.background.parent_stopped()
                return dict(stopped=True, dac=after['dac'], hold_kind='fixed_reference')
            raise BrokerError('action', 'Only typed native lifecycle and bounded offset operations are supported')

    def _configure_input(self, request):
        """PI-off receiver setup without target, gain, reset or output writes."""
        if self.active or self.prepared or self.pending:
            raise BrokerError('busy', 'Input setup requires an unprepared, inactive, clean lease')
        before = self._read_before()
        cfg = before['servo_config']
        route = self.policy['native']['input']
        if cfg['pid_multiplier'] != 0:
            raise BrokerError('busy', 'Input setup requires native PI off')
        if cfg['udp_enable'] != 1 or cfg['udp_port'] != route['udp_port']:
            raise BrokerError('configuration', 'The configured native UDP listener must already be enabled')
        target = number(request.get('target_frequency_hz'), 'target frequency', positive=True)
        limits = acceptance_bounds(self.policy['native'], target)
        values = dict(valid_data_index=route['valid_data_index'],
                      system_min=limits[0], system_max=limits[1])
        after = self._write(values, before, 'configure PI-off wavelength receiver')
        if self.policy['preserved']['multiplier_control'] == 0 and after['dac']['native_code'] != before['dac']['native_code']:
            raise self._latch('PI-off receiver setup changed DAC; lease retained for review')
        self.input_configuration = dict(target_frequency_hz=target,
            acceptance_hz=self.policy['native'].get('acceptance_hz', self.policy['native']['capture_hz']),
            system_bounds_nm=limits, valid_data_index=route['valid_data_index'],
            input_mode='gated' if self.gate is not None else 'direct',
            policy_digest=self.definition, configured_unix_s=time.time(),
            preserved_native_target=cfg['target'], preserved_dac_code=after['dac']['native_code'])
        try:
            self._persist()
        except OSError as exc:
            raise self._latch('Input setup evidence persistence failed: ' + str(exc)) from exc
        self.last_seq = self.last_seq_at = self.last_optical = None
        self.qualified = 0
        return copy.deepcopy(self.input_configuration)

    def _acquire_prepare(self, request):
        """PI-off acquisition context: no firmware target, gains, receiver or reset writes."""
        if self.active or self.pending:
            raise BrokerError('busy', 'PI-off acquisition requires an inactive clean lease')
        c = self.policy['calibration']
        if not c['verified'] or not c['response_verified']:
            raise BrokerError('configuration', 'PI-off acquisition needs verified external-current response')
        before = self._read_before()
        if before['servo_config']['pid_multiplier'] != 0:
            raise BrokerError('busy', 'PI-off acquisition requires actual native PI off')
        target = number(request.get('target_wavelength_nm'), 'target wavelength', positive=True)
        from ldcon.owners.native_policy_safety import actual_envelope
        proof = actual_envelope(self.policy, before['servo_config'])
        self._check_envelope(proof)
        self.prepared = dict(mode='acquisition', target_nm=target, envelope=proof,
                             hardware_writes=False, arm_ready=False)
        return copy.deepcopy(self.prepared)

    def _prepare(self, request):
        if not self.policy['native']['arm_enabled']:
            raise BrokerError('configuration', 'Native arm is disabled until external-path response commissioning')
        if self.active:
            raise BrokerError('busy', 'Cannot reconfigure active native gains or target')
        before = self._read_before()
        if before['servo_config']['pid_multiplier'] != 0:
            raise BrokerError('busy', 'Native preparation requires PI off')
        target = number(request.get('target_wavelength_nm'), 'target wavelength', positive=True)
        from ldcon.owners.native_current_tuning import effective_policy
        p = effective_policy(self.policy, self.saved_pid)
        n = p['native']
        offset = before['servo_config']['control_offset']
        proof = envelope(p, target, offset)
        self._check_envelope(proof)
        raw_limit = n['integral_limit_count'] / abs(n['ki_count_per_nm_s'])
        route = n['input']
        values = dict(valid_data_index=route['valid_data_index'], target=target,
                      system_min=proof['system_bounds_nm'][0], system_max=proof['system_bounds_nm'][1],
                      pid_kp=n['kp_count_per_nm'], pid_ki=n['ki_count_per_nm_s'], pid_kd=0,
                      pid_integral_min=-raw_limit, pid_integral_max=raw_limit,
                      pid_max_interval=1000 * n['interval_bounds_s'][1], pid_reset_state=1,
                      udp_enable=1, udp_port=route['udp_port'],
                      polling_interval=p['preserved']['polling_interval'])
        after = self._write(values, before, 'prepare bounded native profile with PI off')
        if self.policy['preserved']['multiplier_control'] == 0 and after['dac']['native_code'] != before['dac']['native_code']:
            raise self._latch('PI-off preparation changed DAC; retained lease requires review')
        self.prepared = dict(mode='pi', target_nm=target, envelope=proof)
        self.last_seq = self.last_seq_at = self.last_optical = None
        self.qualified = 0
        return copy.deepcopy(self.prepared)

    def _offset(self, request, *, probe=False, acquisition=False):
        if self.active:
            raise BrokerError('busy', 'Open-loop offset changes require PI off')
        before = self._read_before()
        if before['servo_config']['pid_multiplier'] != 0:
            raise BrokerError('busy', 'Open-loop offset requires actual native PI off')
        if acquisition and (not self.prepared or self.prepared.get('mode') != 'acquisition'):
            raise BrokerError('request', 'Prepare a PI-off acquisition context first')
        if not acquisition and not probe and self.prepared and self.prepared.get('mode') == 'acquisition':
            raise BrokerError('request', 'Use the typed acquire_offset operation for acquisition')
        try:
            self._optical()
        except (OSError, DeviceError, ValueError, KeyError, TypeError) as exc:
            raise BrokerError('measurement', 'Fresh independent optical data required before offset') from exc
        value = request.get('code')
        expected = request.get('expected_code')
        if type(value) is not int or not 0 <= value <= 65535:
            raise BrokerError('range', 'DAC offset must be a native integer code')
        old = before['servo_config']['control_offset']
        if old != expected:
            raise BrokerError('measurement', 'Offset changed since caller observation; no write attempted')
        p = self.policy
        elapsed = self.clock() - self.last_write
        if elapsed < p['update_interval_s']:
            raise BrokerError('measurement', 'Offset update interval has not elapsed')
        delta = abs(current_for_code(p, value) - current_for_code(p, int(old))) * (1 + p['calibration']['gain_relative_uncertainty'])
        from ldcon.owners.native_modulation_limits import budget_a
        budget = budget_a(p)
        if budget is not None and delta > budget + 1e-15:
            raise BrokerError('range', 'Offset current step/slew limit exceeded')
        if not self.prepared and not probe:
            raise BrokerError('request', 'Prepare target and envelope first')
        target = self.prepared['target_nm'] if self.prepared else C_NM/self.optical_sample['frequency_hz']
        if acquisition:
            from ldcon.owners.native_policy_safety import actual_envelope
            proof = actual_envelope(p, before['servo_config'], offset=value)
        else:
            proof = envelope(p, target, value, open_loop=probe)
        self._check_envelope(proof)
        after = self._write({'control_offset': value}, before, 'bounded commissioning probe' if probe else 'bounded current offset')
        if self.prepared:
            self.prepared['envelope'] = proof
        return dict(code=value, dac=after['dac'], before_dac=before['dac'],
                    optical_before=copy.deepcopy(self.optical_sample), envelope=proof,
                    response_commissioned=False)

    # ---- rule F generic cards (2026-10-22): live re-centring and live target change while the PI is active ----
    def _hz_per_code(self, wavelength_nm):
        """Signed frequency change per DAC code from the calibration (wavelength rises with current/code)."""
        p = self.policy
        c = p['calibration']
        dac = channel_metadata(p['dac'])
        per_code_v = dac['voltage_step_v'] / (1 << (16 - dac['bits']))
        slope_nm_per_code = c['wavelength_nm_per_a'] * c['current_a_per_v'] * per_code_v
        return -C_NM / wavelength_nm ** 2 * slope_nm_per_code

    def _offset_budget_codes(self):
        from ldcon.owners.native_modulation_limits import budget_codes
        return max(1, budget_codes(self.policy))

    def _recenter(self, request, *, ramp=False):
        """One bounded step of the PI centre (control_offset) while the PI is ACTIVE.

        sr1092 has no outer loop: the PI integral absorbs the shift (output unchanged, integral usage falls by
        delta/limit). The step is bounded by the host offset budget AND the caller's max_step_codes (default 1 code,
        about 1 MHz transient the 1 ms PI removes), the full envelope is re-proved at the new centre, the write is
        readback-verified and the owner's expected configuration follows (no 'external modification' trip).
        ``ramp``: the step is one of the owner's own recenter_ramp (rule L); an outside step is refused meanwhile."""
        if not self.active:
            raise BrokerError('busy', 'Live re-centring needs an active PI (use offset while the PI is off)')
        if self.pid_tuning:
            raise BrokerError('busy', 'Native PID tuning is active')
        if self.retarget and self.retarget.get('active'):
            raise BrokerError('busy', 'A lock target ramp is in progress')
        if not ramp and self.recentre and self.recentre.get('active'):
            raise BrokerError('busy', 'A re-centring ramp is in progress')
        if not self.prepared:
            raise BrokerError('request', 'No prepared target envelope')
        before = self._read_before(require_input=True)
        value = request.get('code')
        expected = request.get('expected_code')
        if type(value) is not int or not 0 <= value <= 65535:
            raise BrokerError('range', 'PI centre must be a native integer code')
        old = before['servo_config']['control_offset']
        if old != expected:
            raise BrokerError('measurement', 'PI centre changed since caller observation; no write attempted')
        p = self.policy
        if self.clock() - self.last_write < p['update_interval_s']:
            raise BrokerError('measurement', 'Update interval has not elapsed')
        max_codes = request.get('max_step_codes', 1)
        if type(max_codes) is not int or not 1 <= max_codes <= self._offset_budget_codes():
            raise BrokerError('range', 'max_step_codes must be 1..%d' % self._offset_budget_codes())
        if abs(value - int(old)) > max_codes:
            raise BrokerError('range', 'Re-centring step exceeds max_step_codes')
        delta = abs(current_for_code(p, value) - current_for_code(p, int(old))) * (1 + p['calibration']['gain_relative_uncertainty'])
        from ldcon.owners.native_modulation_limits import budget_a
        budget = budget_a(p)
        if budget is not None and delta > budget + 1e-15:
            raise BrokerError('range', 'Offset current step/slew limit exceeded')
        from ldcon.owners.native_current_tuning import effective_policy
        try:
            from ldcon.owners.native_policy_safety import actual_envelope
            proof = actual_envelope(p, before['servo_config'], offset=value)
        except ValueError as exc:
            raise BrokerError('range', str(exc)) from exc
        self._check_envelope(proof)
        ff_proof = None
        if self.ff and self.ff['state'] not in ('restored', 'fault'):
            ff_proof = self._ff_recentre_proof(before, value)
        after = self._write({'control_offset': value}, before, 'bounded live re-centring of the PI centre (PI active)'
                            + (' during continuous FF' if ff_proof else ''))
        self.prepared['envelope'] = proof
        if ff_proof is not None:
            s = self.ff
            s['proof'].update(code_bounds=ff_proof['code_bounds'], current_bounds_a=ff_proof['current_bounds_a'],
                              correction_count=ff_proof['correction_count'], maximum_jump_a=ff_proof['maximum_jump_a'])
            entry = dict(time=self.clock(), unix_s=time.time(), code=value, previous=int(old),
                         code_bounds=list(ff_proof['code_bounds']), integral_before=before['servo_status'].get('pid_integral'),
                         reason=str(request.get('reason', ''))[:200])
            s['recentres'] = (s.get('recentres') or [])[-49:] + [entry]
            self._report_event('ff_continuous_recentre', **{k: v for k, v in entry.items() if k != 'time'})
            try:
                self._persist()
            except OSError as exc:
                raise self._latch('Continuous FF re-centre evidence persistence failed: ' + str(exc)) from exc
        return dict(code=value, dac=after['dac'], before_dac=before['dac'], envelope=proof,
                    integral=after['servo_status'].get('pid_integral'), integral_before=before['servo_status'].get('pid_integral'))

    # ---- rule L (2026-10-27): the WHOLE bounded re-centring in ONE RPC -----------------------------------------------
    def _recenter_ramp(self, request):
        """Start a bounded ramp of the PI centre (control_offset) to ``target_code`` while the PI is ACTIVE.

        Everything that can refuse is checked here, before any write: lease/PI state, no other trajectory (target
        ramp, PID tuning, FF session, another ramp), the caller's observation of the present centre, the step size
        (``max_step_codes`` <= the host offset budget; default = the budget) and the full envelope at the FINAL centre.
        poll() then writes one bounded step per update interval through ``_recenter`` (the same per-step checks:
        budget, envelope, readback verification, expected configuration), so the owner's own conditions bound every
        step. Progress/outcome are in status['recentre'] (the readback stream the card judges completion on).
        Not persisted: an owner restart ends the ramp where it is (the card sees the ramp gone and holds)."""
        if not self.active:
            raise BrokerError('busy', 'Live re-centring needs an active PI (use offset while the PI is off)')
        if self.pid_tuning:
            raise BrokerError('busy', 'Native PID tuning is active')
        if self.retarget and self.retarget.get('active'):
            raise BrokerError('busy', 'A lock target ramp is in progress')
        if self.recentre and self.recentre.get('active'):
            raise BrokerError('busy', 'A re-centring ramp is in progress')
        if not self.prepared:
            raise BrokerError('request', 'No prepared target envelope')
        before = self._read_before(require_input=True)
        target = request.get('target_code')
        expected = request.get('expected_code')
        if type(target) is not int or not 0 <= target <= 65535:
            raise BrokerError('range', 'PI centre target must be a native integer code')
        old = int(before['servo_config']['control_offset'])
        if old != expected:
            raise BrokerError('measurement', 'PI centre changed since caller observation; no write attempted')
        budget = self._offset_budget_codes()
        step = request.get('max_step_codes', budget)
        if type(step) is not int or not 1 <= step <= budget:
            raise BrokerError('range', 'max_step_codes must be 1..%d' % budget)
        from ldcon.owners.native_policy_safety import actual_envelope
        try:
            proof = actual_envelope(self.policy, before['servo_config'], offset=target)
        except ValueError as exc:
            raise BrokerError('range', str(exc)) from exc
        self._check_envelope(proof)      # the end point must be inside the activation-relative / absolute windows
        planned = int(math.ceil(abs(target - old) / step)) if target != old else 0
        self.recentre = dict(active=planned > 0, target_code=target, start_code=old, current_code=old, max_step_codes=step,
                             steps=0, steps_planned=planned, started_unix_s=time.time(), finished_unix_s=None if planned else time.time(),
                             error=None, stopped=None, reason=str(request.get('reason', ''))[:200])
        self._report_event('recentre_ramp_started', target_code=target, start_code=old, max_step_codes=step, steps_planned=planned)
        return copy.deepcopy(self.recentre)

    def _recentre_poll(self, state):
        r = self.recentre
        if not r or not r.get('active'):
            return
        if self.clock() - self.last_write < self.policy['update_interval_s'] or self.pending:
            return
        if not state.get('fresh') or self.input_error:
            return
        try:
            current = int(state['sample']['servo_config']['control_offset'])
            remaining = r['target_code'] - current
            if remaining == 0:
                self._recenter_ramp_finish(r, current)
                return
            step = max(-r['max_step_codes'], min(r['max_step_codes'], remaining))
            result = self._recenter(dict(code=current + step, expected_code=current, max_step_codes=r['max_step_codes'],
                                         reason=r.get('reason')), ramp=True)
            r['steps'] += 1
            r['current_code'] = int(result['code'])
            r['last_write_unix_s'] = time.time()
            if r['current_code'] == r['target_code']:
                self._recenter_ramp_finish(r, r['current_code'])
        except BrokerError as exc:
            if exc.code == 'measurement':
                return                     # interval / fresh input / caller-observation race: the next poll retries
            # 'fault' is latched already (the poll loop stops on self.fault); any other refusal ends the ramp here
            r['active'] = False
            r['error'] = str(exc)
            r['finished_unix_s'] = time.time()
            self._report_event('recentre_ramp_ended', steps=r['steps'], current_code=r['current_code'], error=r['error'])
        except (ValueError, KeyError, TypeError) as exc:
            r['active'] = False
            r['error'] = str(exc)
            r['finished_unix_s'] = time.time()
            self._report_event('recentre_ramp_ended', steps=r['steps'], current_code=r['current_code'], error=r['error'])

    def _recenter_ramp_finish(self, r, code):
        r.update(active=False, current_code=int(code), finished_unix_s=time.time())
        self._report_event('recentre_ramp_ended', steps=r['steps'], current_code=r['current_code'], error=None)

    def _recenter_ramp_stop(self, reason):
        """Hold where it is: no further step is written (the steps already written stay; nothing is undone)."""
        r = self.recentre
        if r and r.get('active'):
            r.update(active=False, stopped=reason, finished_unix_s=time.time())
            self._report_event('recentre_ramp_ended', steps=r['steps'], current_code=r['current_code'], error=None, stopped=reason)
        return copy.deepcopy(r)

    def _ff_recentre_proof(self, before, value):
        """v5.8: PI-centre following during an ACTIVE CONTINUOUS FF session. The FF proof is re-made at the new
        control_offset; the FF values (multipliers/baselines/ADC window) must be unchanged, only the reachable codes move."""
        from ldcon.owners.native_ff_proof import prove
        s = self.ff
        if s['state'] != 'active' or not s.get('continuous'):
            raise BrokerError('busy', 'Re-centring during FF is allowed only in an active continuous FF session')
        moved = copy.deepcopy(before)
        moved['servo_config']['control_offset'] = value
        try:
            proof = prove(self.policy, self.ff_profile, s['proof']['plan'], moved)
        except (ValueError, KeyError, TypeError) as exc:
            raise BrokerError('range', 'Continuous FF re-proof failed: ' + str(exc)) from exc
        try:
            self._check_envelope(proof)
        except BrokerError as exc:
            if exc.code != 'range':
                raise
            proof['ok'] = False
            proof['reasons'].append(str(exc))
        if not proof['ok']:
            raise BrokerError('range', 'Continuous FF re-proof at the new centre failed: ' + '; '.join(proof['reasons']))
        if any(not math.isclose(float(proof['values'][k]), float(v), rel_tol=1e-12, abs_tol=1e-12)
               for k, v in s['proof']['values'].items()):
            raise BrokerError('range', 'FF values would change at the new centre; no write attempted')
        return proof

    def _retarget(self, request):
        """Start a bounded live ramp of the firmware target while the PI is ACTIVE (the ONE laser target changed).

        The PI's reach is only +-integral_limit around control_offset, so every ramp step moves the target by at most
        step_hz AND shifts control_offset by the calibrated feed-forward, preserving the integral. No automatic
        integral-neutral recentering is performed. The host offset budget is still enforced; the
        acceptance window follows. The end point must satisfy the full envelope (activation-relative and absolute
        current windows) before anything is written; the ramp runs in poll() at the owner's cadence."""
        if not self.active:
            raise BrokerError('busy', 'Live target change needs an active PI (prepare sets the target while the PI is off)')
        if self.pid_tuning:
            raise BrokerError('busy', 'Native PID tuning is active')
        if self.retarget and self.retarget.get('active'):
            raise BrokerError('busy', 'A lock target ramp is in progress')
        if self.recentre and self.recentre.get('active'):
            raise BrokerError('busy', 'A re-centring ramp is in progress')
        if self.ff and self.ff.get('state') not in ('restored', 'fault'):
            # Symmetric to ff_begin refusing during a ramp: the FF proof is made at the current target/offset.
            raise BrokerError('busy', 'A feedforward session owns this native lease; change the target after it is restored')
        if not self.prepared:
            raise BrokerError('request', 'No prepared target envelope')
        target_hz = number(request.get('target_frequency_hz'), 'target frequency', positive=True)
        current_hz = C_NM / self.prepared['target_nm']
        n = self.policy['native']
        settings = self.target_ff_study.settings(request.get('target_ff_study_token'), *self._target_ff_study_context())
        step_hz = request.get('step_hz', settings['step_hz'])
        step_hz = number(step_hz, 'step_hz', positive=True)
        if step_hz > settings['step_hz']:
            raise BrokerError('range', 'step_hz exceeds the authorized target-ramp ceiling')
        before = self._read_before(require_input=True)
        offset = before['servo_config']['control_offset']
        hz_per_code = self._hz_per_code(C_NM / target_hz)
        scale = settings['scale']
        final_offset = int(round(offset + scale * (target_hz - current_hz) / hz_per_code))
        if not 0 <= final_offset <= 65535:
            raise BrokerError('range', 'Target change would leave the DAC range')
        from ldcon.owners.native_current_tuning import effective_policy
        try:
            from ldcon.owners.native_policy_safety import running_gain_policy
            proof = envelope(running_gain_policy(self.policy, before['servo_config']), C_NM / target_hz, final_offset)
        except ValueError as exc:
            raise BrokerError('range', str(exc)) from exc
        self._check_envelope(proof)      # refuses a change that leaves the activation-relative / absolute windows
        status = before['servo_status']
        start_neutral = float(offset) + float(status.get('pid_output', 0.) or 0.)
        self.retarget = dict(active=True, final_hz=target_hz, current_hz=current_hz, start_hz=current_hz,
                             step_hz=step_hz, steps=0, started_unix_s=time.time(), final_offset_estimate=final_offset,
                             hz_per_code=hz_per_code, target_feedforward_scale=scale,
                             error=None, start_neutral_code=start_neutral,
                             start_offset=int(offset), strategy='target_feedforward', automatic_recentre=False,
                             owner_recentre='hybrid_background', recentre_integral_fraction=recentre_integral_fraction(n),
                             integral_fraction_before=self._integral_fraction(before),
                             dac_headroom_hz=dac_headroom(self.policy, before['servo_config'], hz_per_code, scale=scale,
                                                          dac_limits=self.dac_limits, activation_code=self.activation_code),
                             neutral_corrections=0, correction_codes=0, trims=0)
        return copy.deepcopy(self.retarget)

    def _target_ff_study_context(self):
        return self.lease, self.policy['native'], self.definition

    def _target_ff_study_dispatch(self, request):
        action = request['action']
        if action == 'target_ff_study_status':
            result = self.target_ff_study.status(*self._target_ff_study_context())
            move = self.retarget
            if move is not None:
                fields = ('steps','started_unix_s','finished_unix_s','first_write_started_unix_s',
                          'first_write_verified_unix_s','last_write_verified_unix_s',
                          'minimum_actual_step_hz','maximum_actual_step_hz','target_feedforward_scale')
                result['last_move'] = {k:move[k] for k in fields if type(move.get(k)) in (int,float) and math.isfinite(move[k])}
                result['last_move'].update(target_frequency_hz=move['final_hz'],ramped_target_hz=move['current_hz'],
                    target_ramp_step_hz=move['step_hz'],target_finished=not move['active'] and not move.get('error'))
            else:
                result['last_move'] = None
            return result
        if action == 'target_ff_study_restore':
            return self.target_ff_study.restore(request, *self._target_ff_study_context())
        if (not self.active or self.pid_tuning or self.pending
                or self.retarget and self.retarget.get('active')
                or self.ff and self.ff.get('state') not in ('restored', 'fault')):
            raise BrokerError('busy', 'Target FF study requires an active idle native controller')
        self.background.boundary(action)
        result = self.target_ff_study.set(request, *self._target_ff_study_context())
        self._report_event('target_ff_study_policy', revision=result['revision'],
                           session=result['session'], hardware_write=False)
        return result

    def _retarget_transition_proof(self, before, target_nm, new_offset, system_bounds):
        """Cover either ordering of a non-atomic target/window/offset update."""
        from ldcon.owners.native_policy_safety import codes_proof
        cfg = before['servo_config']
        systems = [cfg['system_min'], cfg['system_max'], *system_bounds]
        targets = [cfg['target'], target_nm]
        correction = abs(cfg['pid_kp']) * max(abs(x-y) for x in systems for y in targets)
        correction += abs(cfg['pid_ki']) * max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
        adc = [cfg['mod_valid_min'], cfg['mod_valid_max']]
        ff = [0., *(cfg['multiplier_control']*(x-cfg['baseline_control']) for x in adc)]
        offsets = [cfg['control_offset'], new_offset]
        held = before['dac']['native_code']
        proof = codes_proof(self.policy,
            [min(held, min(offsets)+min(ff)-correction), max(held, max(offsets)+max(ff)+correction)],
            source='target_window_offset_update_order')
        self._check_envelope(proof)
        return proof

    def _retarget_finish(self, r):
        # Write completion is not optical settling or a new calibration measurement.
        r['active'] = False
        r['finished_unix_s'] = time.time()
        r['completion_basis'] = 'configuration_readback'
        # Hybrid integral recentre (decision 5, 2026-10-27): judged at the next poll, in the background.
        r['recentre_check'] = 'pending' if recentre_integral_fraction(self.policy['native']) > 0 else 'disabled'

    def _retarget_poll(self, state):
        r = self.retarget
        if not r or not r.get('active'):
            return
        if self.clock() - self.last_write < self.policy['update_interval_s'] or self.pending:
            return
        if not state.get('fresh') or self.input_error:
            return
        try:
            remaining = r['final_hz'] - r['current_hz']
            if abs(remaining) < 1e-9:
                self._retarget_finish(r)
                return
            before = self._read_before(require_input=True)
            offset = int(before['servo_config']['control_offset'])
            budget = self._offset_budget_codes()
            scale = r.get('target_feedforward_scale', 1.)
            maximum = min(r['step_hz'], budget * abs(r['hz_per_code']) / scale)
            step = max(-maximum, min(maximum, remaining))
            # Calculate from this request's origin, not from successive rounded steps.
            # Keep the existing raw integral and its clamps; never add pid_output
            # into the reference and never queue integral-neutral trims here.
            for _ in range(32):
                new_hz = r['current_hz'] + step
                new_offset = int(round(r['start_offset'] + scale * (new_hz-r['start_hz']) / r['hz_per_code']))
                if abs(new_offset-offset) <= budget:
                    target_nm = C_NM / new_hz
                    from ldcon.owners.native_policy_safety import running_gain_policy
                    proof = envelope(running_gain_policy(self.policy, before['servo_config']), target_nm, new_offset)
                    self._check_envelope(proof)
                    try:
                        transition = self._retarget_transition_proof(before, target_nm, new_offset, proof['system_bounds_nm'])
                    except BrokerError as exc:
                        if exc.code != 'range':
                            raise
                    else:
                        break
                step *= .5
            else:
                raise BrokerError('range', 'Target feedforward cannot meet the current step budget')
            bounds = proof['system_bounds_nm']
            write_started = time.time()
            after = self._write(dict(target=target_nm, system_min=bounds[0], system_max=bounds[1], control_offset=new_offset),
                                before, 'target and calibrated current feedforward (%+.3f MHz, %+d codes)' %
                                (step / 1e6, new_offset-offset))
            verified = time.time()
            actual_step = abs(new_hz-r['current_hz'])
            r.setdefault('first_write_started_unix_s', write_started)
            r.setdefault('first_write_verified_unix_s', verified)
            r['last_write_verified_unix_s'] = verified
            self.prepared = dict(target_nm=target_nm, envelope=proof)
            r['current_hz'] = new_hz
            r['steps'] += 1
            r['minimum_actual_step_hz'] = min(r.get('minimum_actual_step_hz',actual_step),actual_step)
            r['maximum_actual_step_hz'] = max(r.get('maximum_actual_step_hz',actual_step),actual_step)
            r['verified_offset'] = int(after['servo_config']['control_offset'])
            r['last_transition_proof'] = transition
            if abs(r['final_hz']-new_hz) < 1e-9:
                self._retarget_finish(r)
        except BrokerError as exc:
            if exc.code == 'measurement':
                return
            if exc.code == 'fault':
                raise
            r['active'] = False
            r['error'] = str(exc)
        except (ValueError, KeyError, TypeError) as exc:
            r['active'] = False
            r['error'] = str(exc)

    def _ff_dispatch(self, request):
        """FF attaches to this lease; no second ESP or CLD ownership path."""
        from ldcon.owners.native_ff_proof import firmware_snapshot, prove
        action = request['action']
        if not self.ff_profile or not self.ff_profile['enabled']:
            raise BrokerError('configuration', 'Native feedforward has no enabled approved profile')
        if request.get('profile_digest') != self.ff_definition:
            raise BrokerError('configuration', 'Exact native FF profile digest required')
        if action in ('ff_status', 'ff_heartbeat', 'ff_end'):
            if not self.ff or (request.get('token') != self.ff['token']
                    and not (action == 'ff_status' and request.get('token') is None)):
                raise BrokerError('ownership', 'Exact native feedforward token required')
            if action == 'ff_heartbeat':
                self.ff['heartbeat'] = self.clock()
            elif action == 'ff_end':
                self._ff_request_end(str(request.get('reason', 'Requested end'))[:512])
            return copy.deepcopy(self.ff)
        if not self.active or not self.prepared or self.pending or self.pid_tuning:
            raise BrokerError('busy', 'FF requires an active, clean native lease without PID tuning')
        if self.ff and self.ff['state'] != 'restored':
            raise BrokerError('busy', 'An FF session already owns this native lease')
        if self.retarget and self.retarget.get('active'):
            raise BrokerError('busy', 'A lock target ramp is in progress')
        if self.recentre and self.recentre.get('active'):
            raise BrokerError('busy', 'A re-centring ramp is in progress')
        before = self._read_before(require_input=True)
        try:
            proof = prove(self.policy, self.ff_profile, request.get('plan'), before)
        except (ValueError, KeyError, TypeError) as exc:
            raise BrokerError('configuration', str(exc)) from exc
        try:
            self._check_envelope(proof)
        except BrokerError as exc:
            if exc.code != 'range':
                raise
            proof['ok'] = False
            proof['reasons'].append(str(exc))
        proof['policy_digest'] = self.definition
        proof.pop('proof_digest', None)
        proof['proof_digest'] = digest(proof)
        if action == 'ff_validate':
            return proof
        if not proof['ok']:
            raise BrokerError('range', '; '.join(proof['reasons']))
        if request.get('proof_digest') != proof['proof_digest']:
            raise BrokerError('configuration', 'Begin requires the current reviewed FF proof digest')
        snap = firmware_snapshot(before)
        p = self.ff_profile
        center = p['adc_center_count']
        # v5.3: every sample of the continuous window (cleared on any invalid poll, <= 2 x start_span_s long) must be at
        # the centre; at least start_samples of them, spanning at least start_span_s. Firmware-side validity only
        # (ADC valid/has-valid/IO-valid, invalid counter unchanged, consumed PID sample); no wavemeter quality.
        samples = list(self.ff_center_samples)
        if (len(samples) < p['start_samples'] or samples[-1]['time']-samples[0]['time'] < p['start_span_s']
                or self.clock()-samples[-1]['time'] > self.policy['max_sample_age_s']
                or any(abs(s['raw']-center) > p['adc_noise_counts'] for s in samples)
                or len({s['invalid'] for s in samples}) != 1
                or not snap['mod_valid'] or not snap['mod_has_valid'] or not snap['adc_io_valid']
                or snap['mod_invalid'] != samples[-1]['invalid']
                or abs(snap['adc_raw']-center) > p['adc_noise_counts']):
            raise BrokerError('measurement', 'Fresh stable physical ADC center samples required before FF enable')
        cfg = before['servo_config']
        raw_limit = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
        if abs(snap['pid_integral']) > p['start_integral_fraction_max']*raw_limit:
            raise BrokerError('measurement', 'Native integral has insufficient FF start reserve')
        now = self.clock()
        self.ff = dict(token=uuid.uuid4().hex, state='enabling', reason=None, proof=proof,
            profile_digest=self.ff_definition, plan_digest=proof['plan_digest'],
            original={k: cfg[k] for k in proof['values']}, original_controlled=dict(self.expected_config),
            started=now, heartbeat=now, last_fresh=now, last_cycle=snap['cycle_seq'],
            last_cycle_time_us=snap['cycle_time_us'], invalid_count=snap['mod_invalid'],
            target_match_overflow=snap['target_match_overflow'], last_pid_cycle=snap['pid_cycle_seq'],
            last_system_seq=snap['system_seq'], last_write_system_seq=snap['system_seq'],
            step=0, steps=p['unwind_steps'], next_step=now, last_acquisition=self.last_optical,
            unwind_timeout_s=p['unwind_steps']*p['unwind_interval_s']+p['missing_timeout_s']+2*self.interval_s,
            # v5.8: continuous playback (one repeated period); lifetime = heartbeat-bounded up to max_continuous_s
            continuous=bool(proof.get('continuous')),
            continuous_cap_s=proof.get('max_continuous_s') if proof.get('continuous') else None,
            continuous_until_unix_s=(time.time()+proof['max_continuous_s']+proof.get('start_window_s', 0.))
                                    if proof.get('continuous') else None,
            recentres=[])
        self.ff['adc_log'] = self._adc_log_apply(request.get('plan') or {}, proof, samples)
        self._ff_write(proof['values'], before, 'enable coherent native feedforward')
        self.ff['state'] = 'active'
        try:
            self._persist()
        except OSError as exc:
            raise self._latch('FF enable evidence persistence failed: '+str(exc)) from exc
        self.ff_center_samples.clear()
        return copy.deepcopy(self.ff)

    def _ff_write(self, values, before, reason):
        from ldcon.owners.native_instrument_owner import NativePrewriteRefusal
        from ldcon.owners.native_ff_proof import FF_KEYS, firmware_snapshot
        prior = dict(self.expected_preserved)
        snapshot = firmware_snapshot(before)
        v1 = snapshot.get('contract', 'v1') == 'v1'
        revision = snapshot['config_revision'] if v1 else None
        # The shipped firmware has no revision: the FF keys go out as one atomic
        # modulation group (multipliers/baselines in one /ws message; the ADC validity
        # window and target time matching beforehand) and are verified by readback.
        time_match = 0 if all(values[k] == self.policy['preserved'][k] for k in ('multiplier_control', 'multiplier_target')) else 1
        self.pending = dict(reason=reason, feedforward=values, expected_revision=revision,
                            profile_digest=self.ff_definition, target_time_match=time_match)
        intent = copy.deepcopy(self.pending)
        try:
            self._persist()
        except OSError as exc:
            raise self._latch('FF write-intent persistence failed: '+str(exc)) from exc
        written = False
        try:
            if v1:
                self.driver.set_feedforward(values, expected_revision=revision,
                                            profile_digest=self.ff_definition)
                written = True
            else:
                window = dict(mod_valid_min=values['mod_valid_min'], mod_valid_max=values['mod_valid_max'])
                if time_match:
                    self.driver.set_control(dict(window, target_time_match=1))
                    written = True
                self.driver.set_control({k: values[k] for k in ('multiplier_control', 'baseline_control',
                                                                'multiplier_target', 'baseline_target')})
                written = True
                if not time_match:
                    self.driver.set_control(dict(window, target_time_match=0))
            after = self.driver.read()
            snap = firmware_snapshot(after)
            expected = dict(values, target_time_match=time_match) if not v1 else values
            if v1 and (snap['config_revision'] != revision+1 or snap['profile_digest'] != self.ff_definition):
                raise ValueError('Atomic FF commit verification failed')
            if any(not math.isclose(float(after['servo_config'].get(k, math.nan)), float(v), rel_tol=1e-6, abs_tol=1e-9)
                   for k, v in expected.items()):
                raise ValueError('Atomic FF commit verification failed')
            # Expect what the firmware actually keeps (float32 multipliers; verified close above), not the double sent
            # (2026-10-22 12:27 EDT incident: the exact compare in _identity latched a verified write as foreign).
            self.expected_preserved.update({k: after['servo_config'][k] for k in expected})
            self.ff['expected_revision'] = revision+1 if v1 else None
            self._identity(after)
            self.pending = None
            self.last_write = self.clock()
            self._persist()
            with self.lock:
                self.state.update(sample=after, read_ok=True, state='connected',
                                  last_success_unix_s=time.time(), error=None, failures=0)
            return after
        except Exception as exc:
            if isinstance(exc, NativePrewriteRefusal) and not written:
                self.pending = None
                self._persist()
                raise
            self.pending = intent
            self.expected_preserved = prior
            self.ff['state'] = 'fault'
            reason = 'Uncertain atomic FF write; do not replay; lease retained: '+str(exc)
            # A separate bounded emergency PI disable, never a replay or FF
            # restore. Retain the original intent and both leases for review.
            if self.active:
                try:
                    self.driver.emergency_disable()
                    stopped = self.driver.read()
                    if stopped['servo_config']['pid_multiplier'] != 0:
                        raise ValueError('Emergency PI disable not verified')
                    self.active = False
                    reason += '; emergency PI disable verified'
                except Exception as stop_error:
                    reason += '; emergency PI disable uncertain: '+str(stop_error)
            raise self._latch(reason) from exc

    # ---- temporary ADC log threshold (2026-10-23, telemetry only; never a fault, never in the FF expectations) ----
    def _adc_log_io(self):
        return dict(write=self.driver.set_control, read_config=lambda: self.driver.get('/config'),
                    read_status=lambda: self.driver.get('/status'))

    def _adc_log_apply(self, plan, proof, samples):
        from ldcon.owners.adc_log_threshold import apply, public, sigma_counts
        from ldcon.owners.native_ff_proof import hz_per_v, levels
        try:
            adc = self.policy['adc']
            counts_per_v = 65535. / (adc['max_voltage'] - adc['min_voltage'])     # mod_raw is the 16-bit native code
            counts_per_hz = counts_per_v / abs(hz_per_v(self.ff_profile, plan))
            record = apply(self.adc_log, levels_hz=levels(plan), counts_per_hz=counts_per_hz,
                           sigma=sigma_counts([s['raw'] for s in samples]),
                           context=dict(plan_digest=proof.get('plan_digest'), route='native_ff',
                                        device_id=self.driver.device_id), **self._adc_log_io())
        except Exception as error:      # noqa: BLE001 - telemetry only
            record = dict(state='failed', error=str(error)[:300])
        return public(record)

    def _adc_log_tick(self):
        """Restore the original threshold once no FF session is running (normal end, abort/unwind, or the first poll
        after an owner restart). Not while the owner is in fault (quarantine: nothing is written)."""
        journal = getattr(self, 'adc_log', None)
        if journal is None or self.fault or not journal.needs_restore():
            return
        if self.ff and self.ff.get('state') != 'restored':
            return
        from ldcon.owners.adc_log_threshold import public, restore
        record = restore(journal, reason='FF session ended' if self.ff else 'owner start recovery', **self._adc_log_io())
        if self.ff is not None and self.ff.get('adc_log') is not None:
            self.ff['adc_log'] = public(record)
            try:
                self._persist()
            except OSError:
                pass

    def _ff_request_end(self, reason):
        if self.ff['state'] in ('restored', 'fault', 'unwinding'):
            return
        self.ff.update(state='unwinding', reason=reason, next_step=self.clock(),
                       unwind_started=self.clock())
        try:
            self._persist()
        except OSError as exc:
            raise self._latch('FF unwind intent persistence failed: '+str(exc)) from exc

    def _ff_poll(self, state):
        from ldcon.owners.native_ff_proof import firmware_snapshot, qualified_pid_sample
        p = self.ff_profile
        running = self.ff and self.ff['state'] not in ('restored', 'fault')
        now = self.clock()
        if self.input_error == SEQ_REGRESSED:
            return                       # that one sample is refused; no FF decision is made on it
        try:
            if not state.get('fresh') or self.input_error is not None:
                raise ValueError('Native input unavailable')
            snap = firmware_snapshot(state['sample'])
        except (ValueError, KeyError, TypeError) as exc:
            self.ff_center_samples.clear()
            if running:
                self._ff_request_end('FF observation unavailable: '+str(exc))
                if now-self.ff['last_fresh'] >= p['missing_timeout_s']:
                    self.ff['state'] = 'fault'
                    self._fatal('FF fresh-input unwind deadline exceeded')
            return
        if not running:
            if (self.active and snap['mod_valid'] and snap['mod_has_valid'] and snap['adc_io_valid']
                    and qualified_pid_sample(snap, self.policy['max_sample_age_s'])):
                if self.ff_center_samples and snap['mod_invalid'] != self.ff_center_samples[-1]['invalid']:
                    # v5.10 (owner session 2026-10-23): the invalid counter changed (e.g. the DC->ARB carrier change at
                    # arm, ~16 counts): restart the stability count from this sample instead of keeping the older
                    # samples until they age out of the 2 x start_span_s window (that waited up to 4 s and gave the
                    # +3-4.5 s ARM variance). The evidence is unchanged: start_samples samples at the centre with ONE
                    # invalid count spanning start_span_s, all after the last change.
                    self.ff_center_samples = []
                    self.ff_center_restarts = getattr(self, 'ff_center_restarts', 0) + 1
                if not self.ff_center_samples or snap['cycle_seq'] > self.ff_center_samples[-1]['cycle']:
                    self.ff_center_samples.append(dict(time=now, cycle=snap['cycle_seq'],
                        raw=snap['adc_raw'], invalid=snap['mod_invalid']))
                    # v5.3: keep a TIME window (2 x start_span_s), not the last start_samples entries. Trimming to 5
                    # entries made the >= start_span_s requirement depend on the owner poll period: it could pass only
                    # with polls >= start_span_s/(start_samples-1) = 0.5 s apart. 2026-10-22 18:45-18:47Z the polls
                    # ran every ~0.4 s, the 5 kept samples spanned ~1.6 s, and three ff_begin attempts were refused
                    # for 30 s each although the ADC sat at the centre (19526-19532).
                    keep_from = now - 2. * p['start_span_s']
                    window = [x for x in self.ff_center_samples if x['time'] >= keep_from]
                    if len(window) < p['start_samples']:
                        window = self.ff_center_samples[-p['start_samples']:]
                    self.ff_center_samples = window[-1000:]
            else:
                self.ff_center_samples.clear()
            return
        s = self.ff
        if snap['cycle_seq'] < s['last_cycle'] or snap['cycle_time_us'] < s['last_cycle_time_us']:
            s['state'] = 'fault'
            self._fatal('FF firmware cycle regressed; possible device restart')
            return
        if snap['cycle_seq'] == s['last_cycle']:
            if now-s['last_fresh'] >= p['missing_timeout_s']:
                s['state'] = 'fault'
                self._fatal('FF cycle freshness deadline exceeded')
            return
        s.update(last_cycle=snap['cycle_seq'], last_cycle_time_us=snap['cycle_time_us'])
        matched = qualified_pid_sample(snap, self.policy['max_sample_age_s'])
        new_pid = snap['system_seq'] > s['last_system_seq'] and snap['pid_cycle_seq'] > s['last_pid_cycle']
        if snap['system_seq'] < s['last_system_seq'] or snap['pid_cycle_seq'] < s['last_pid_cycle']:
            s['state'] = 'fault'
            self._fatal('Consumed FF PID input sequence regressed')
            return
        if matched and new_pid:
            s.update(last_fresh=now, last_system_seq=snap['system_seq'], last_pid_cycle=snap['pid_cycle_seq'])
        # The first ADC cycle after atomic commit can legitimately retain the
        # prior revision's PID sample. Hold comparison/rollback until a new
        # consumed sample arrives; never certify the old one as a new result.
        if now-s['last_fresh'] >= p['missing_timeout_s']:
            s['state'] = 'fault'
            self._fatal('FF consumed-input freshness deadline exceeded')
            return
        if snap['mod_invalid'] < s['invalid_count']:
            s['state'] = 'fault'
            self._fatal('FF invalid counter regressed')
            return
        cfg = state['sample']['servo_config']
        raw_limit = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
        if not s['proof']['code_bounds'][0] <= state['sample']['dac']['native_code'] <= s['proof']['code_bounds'][1]:
            s['state'] = 'fault'
            self._fatal('DAC left proven FF reachable envelope')
            return
        valid = True
        if valid and (snap['system_value'] <= 0 or snap['target_for_pid'] <= 0):
            self._ff_request_end('No valid coherent FF error comparison')
        elif (snap['target_match_overflow'] != s['target_match_overflow']):
            self._ff_request_end('Native target matching overflow counter changed')
        elif valid and matched and new_pid and abs(C_NM/snap['system_value']-C_NM/snap['target_for_pid']) > p['abort_error_hz']:
            self._ff_request_end('Same-cycle native FF error exceeded approved limit')
        elif valid and matched and new_pid and abs(snap['pid_integral']) >= p['abort_integral_fraction_max']*raw_limit:
            self._ff_request_end('Native integral reached approved FF abort fraction')
        elif (snap['mod_invalid'] != s['invalid_count'] or not snap['mod_valid']
              or not snap['mod_has_valid'] or not snap['adc_io_valid']):
            # v5.4: say which firmware condition and with what values (2026-10-22 18:59Z R4: mod_invalid 42 -> 54 during
            # an armed 12.3 s record, invisible in the 22 Hz VM series)
            self._ff_request_end('Physical ADC invalid or invalid counter advanced (mod_invalid %s->%s, mod_valid %s, '
                                 'has_valid %s, adc_io_valid %s, adc_raw %s, adc_glitches %s)'
                                 % (s['invalid_count'], snap['mod_invalid'], snap['mod_valid'], snap['mod_has_valid'],
                                    snap['adc_io_valid'], snap.get('adc_raw'), snap.get('adc_glitches')))
        elif now-s['heartbeat'] > p['heartbeat_timeout_s']:
            self._ff_request_end('FF client heartbeat expired')
        elif s.get('continuous') and now-s['started'] > s['continuous_cap_s']+s['proof'].get('start_window_s', 0.):
            # v5.8: a continuous record repeats until stopped; its lifetime is the client heartbeat (above) up to the cap
            self._ff_request_end('FF continuous lifetime cap reached')
        elif not s.get('continuous') and now-s['started'] > s['proof']['duration_s']+s['proof'].get('start_window_s', 0.)+p['heartbeat_timeout_s']:
            # v5.5: the lifetime covers the plan's bounded start window (arm -> READY -> trigger wait) + the record
            self._ff_request_end('FF waveform lifetime expired')
        if s['state'] != 'unwinding' or now < s['next_step']:
            return
        if now-s['unwind_started'] > s['unwind_timeout_s']:
            s['state'] = 'fault'
            self._fatal('FF bounded unwind deadline exceeded')
            return
        try:
            before = self._read_before(require_input=True)
            try:
                before_ff = firmware_snapshot(before)
            except (ValueError, KeyError, TypeError) as exc:
                raise BrokerError('measurement', 'Prewrite coherent FF snapshot unavailable: '+str(exc)) from exc
            # Each rollback uses a new qualified optical acquisition, even
            # when status GETs return many new ADC cycles within one WLM point.
            if (self.last_optical == s['last_acquisition']
                    or not qualified_pid_sample(before_ff, self.policy['max_sample_age_s'])
                    or before_ff['system_seq'] <= s['last_write_system_seq']):
                return
            factor = max(0., 1.-(s['step']+1)/s['steps'])
            # v5.8 (human approval 2026-10-23 ~04:45Z; sweep-speed study): when the physical modulation input is already at
            # the centre (|adc_raw - baseline_control| <= adc_noise_counts on this fresh, qualified snapshot), every FF
            # term is ~0 (mc*(i-bc) and mt*(i-bt) bounded by the approved ADC noise), so the remaining unwind steps move
            # nothing: restore the original values in this one step. Still exactly one write, one new optical
            # acquisition and the same readback/identity checks; an abort mid-record (ADC away from the centre) keeps the
            # bounded multi-step unwind.
            centre = s['proof']['values']['baseline_control']
            if (abs(before_ff['adc_raw'] - centre) <= p['adc_noise_counts'] and before_ff.get('mod_valid')
                    and before_ff.get('adc_io_valid')):
                factor = 0.
                s['unwind_at_centre'] = True
            values = dict(s['proof']['values'])
            values['multiplier_control'] *= factor
            values['multiplier_target'] *= factor
            if factor == 0:
                values = dict(s['original'])
            self._ff_write(values, before, 'coherent native feedforward unwind')
        except BrokerError as exc:
            if exc.code == 'measurement':
                return  # No physical write occurred. Never replay an old proposal.
            raise
        s.update(step=s['step']+1, next_step=now+p['unwind_interval_s'],
                 last_acquisition=self.last_optical, last_write_system_seq=before_ff['system_seq'])
        if factor == 0:
            self.expected_preserved = dict(self.policy['preserved'])
            s.update(state='restored', restored_unix_s=time.time(), native_pi_unchanged=self.active)
        self._persist()
