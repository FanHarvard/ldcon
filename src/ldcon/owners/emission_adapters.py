"""Physical emission adapters constructed only inside the laser owners."""
import json
import math
import time
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.owners.emission import EmissionController, EmissionStore, finite, monitor_sample
from ldcon.core.instrument_client import BrokerError, RemoteCLD1015


class NativeEmissionAdapter:
    def __init__(self, system):
        self.system = system
        self.controller = None

    @property
    def mutex(self):
        return self.runtime.io_lock

    @property
    def stopping(self):
        return self.runtime.stop.is_set()

    @property
    def runtime(self):
        return self.system.runtime

    def status(self):
        from ldcon.owners.esp32_native_client import BiasLease
        return BiasLease(self.system.policy['cld_interlock'])._rpc('status')

    def preflight(self, p):
        if not self.system.policy['writes_enabled'] or not self.system.policy['calibration']['verified']:
            raise BrokerError('configuration','Native laser output writes/calibration are not enabled')
        s = self.status()
        binding = s.get('emission_interlock') or {}
        if (p['method'] != 'driver_enable' or binding.get('path') != p['interlock_file'] or
                binding.get('laser_id') != self.system.authority.system_id or
                binding.get('ld_enable_writes_enabled') is not True):
            raise BrokerError('configuration', 'CLD owner must share this exact emission interlock and explicit LD-only enable grant')
        if s.get('writes_enabled') is not True:
            raise BrokerError('configuration','CLD physical write binding is disabled')
        if s.get('fault') or s.get('fresh') is not True:
            raise BrokerError('measurement', 'Fresh fault-free CLD current readback is required')
        self.system.emission.check_current(self.snapshot(p)['current'],p)

    def quiet(self, p):
        r = self.runtime
        if r.fault or r.lease or r.active or r.pending or r.bias.lease or self.system.assemblies.active():
            raise BrokerError('busy', 'Native PI, assembly and bias lease must finish normal cleanup before emission change')
        s = r._read_before()
        if any(s['servo_config'].get(k) != 0 for k in
                ('pid_multiplier','multiplier_control','multiplier_target','software_modulation_enable')):
            raise BrokerError('busy', 'Native PI/FF stop has not been independently verified')

    def open(self, p):
        from ldcon.drivers.cld1015_config import configuration
        c = self.system.policy['cld_interlock']
        self.controller = RemoteCLD1015(configuration(resource=c['resource'], endpoint=c['endpoint'], instrument=c['instrument']))
        self.controller.connect()  # Exclusive controller lease, never a second USB connection.

    def snapshot(self, p):
        s = self.status()
        if s.get('fresh') is not True or s.get('fault'):
            raise BrokerError('measurement', 'CLD current cache is unavailable')
        row = s['sample']; stamp = s['observed_at']
        return dict(current=dict(current_a=row['current_read_a'], acquired_unix_s=stamp,
                                 acquisition_id=str(stamp), uncertainty_a=p['current_uncertainty_a']),
                    ld_enabled=row['ld_enabled'], tec_enabled=row['tec_enabled'],
                    temperature_c=row['temperature_c'], temperature_read_c=row['temperature_read_c'],
                    modulation_enabled=row['modulation_enabled'], current_setpoint_a=row['current_a'])

    def validate_transition(self, p, desired, before):
        if desired == 'on' and before['tec_enabled'] is not True:
            raise BrokerError('range', 'TEC must remain enabled before LD ON')
        if desired == 'on':
            policy=self.system.policy
            if not math.isclose(before['current_setpoint_a'],policy['calibration']['bias_current_a'],abs_tol=1e-9,rel_tol=0):
                raise BrokerError('configuration','CLD bias differs from the commissioned native current calibration')
            from ldcon.owners.native_policy_safety import actual_envelope,check_envelope
            sample=self.runtime._read_before()
            proof=check_envelope(self.runtime,policy,actual_envelope(policy,sample['servo_config']),sample)
            low,high=p['current_bounds_a']
            if not low <= proof['current_bounds_a'][0] <= proof['current_bounds_a'][1] <= high:
                raise BrokerError('range','Native held output exceeds the emission current envelope')
        # CLD's existing validate/physical projection independently enforce its
        # current setpoint, temperature, modes and hardware current limit.

    def apply(self, p, desired, permit):
        self.system.emission.check_cancelled()
        self.controller._call('enable', output='ld', enabled=desired=='on', emission_permit=permit)
        return time.time()

    def verify_preserved(self, before, after, desired):
        for key in ('tec_enabled', 'temperature_c', 'modulation_enabled', 'current_setpoint_a'):
            if before[key] != after[key]:
                raise BrokerError('fault', 'CLD preserved setting changed: ' + key)
        if after['ld_enabled'] is not (desired == 'on'):
            raise BrokerError('measurement', 'CLD LD-enable readback does not match the request')

    def close(self):
        self.controller.close()
        self.controller = None


class ECDLEmissionAdapter:
    def __init__(self, owner):
        self.owner = owner
        self.http = build_opener(ProxyHandler({}))
        self.before = None
        self.policy = None
        self.expected_voltage = None

    @property
    def mutex(self):
        return self.owner.lock

    def read_current(self, p):
        request = Request(p['monitor']['endpoint'].rstrip('/') + '/v1/current')
        # A monitor has its own owning process. Never open a second scope session
        # or change its selected channel/acquisition settings from this adapter.
        try:
            with self.http.open(request, timeout=min(5., p['max_sample_age_s'])) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise BrokerError('measurement', 'Current-monitor response exceeds 64 KiB')
            return monitor_sample(json.loads(raw), p['monitor'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BrokerError('measurement', 'Current monitor is unavailable or invalid; no output attempted') from error

    def preflight(self, p):
        if self.owner.laser['allow_output'] is not True:
            raise BrokerError('configuration','Laser output writes are not enabled')
        if p['method'] != 'current_modulation':
            raise BrokerError('configuration', 'ECDL emission requires the current-modulation adapter')
        axes = self.owner.policies()
        a, m = axes['current'], p['modulation']
        if not all(a.low <= m[k] <= a.high for k in ('on_voltage_v', 'off_voltage_v')):
            raise BrokerError('range', 'Emission endpoints require approval inside the laser absolute current-modulation bounds')
        if m['max_step_v'] > a.max_step or m['max_slew_v_per_s'] > a.max_slew_per_s or m['update_interval_s'] < a.update_interval_s:
            raise BrokerError('range', 'Emission trajectory exceeds the current axis step/slew/update policy')
        self.owner.emission.check_current(self.read_current(p),p)

    def quiet(self, p):
        o = self.owner
        if o.fault or o.lease or o.steady.armed or o.steady.sweep is not None and o.steady.sweep.state not in ('restored','closed','cancelled'):
            raise BrokerError('busy', 'ECDL PI, background/sweep and lease cleanup must finish before emission change')
        state = o.hw().snapshot()
        if not state['open_loop']:
            raise BrokerError('busy', 'ECDL PI/FF stop has not been independently verified')
        o.validate_state(state, o.policies())

    def open(self, p):
        self.policy = p
        self.before = self.owner.hw().snapshot()
        self.reserved = self.owner.hw().reserved_readbacks()

    def snapshot(self, p):
        s = self.owner.hw().snapshot()
        return dict(current=self.read_current(p), outputs=s['outputs'], open_loop=s['open_loop'],
                    current_channel=s['current'], reserved=self.owner.hw().reserved_readbacks())

    def validate_transition(self, p, desired, before):
        s = before['current_channel']
        if s.get('enabled') is not True or s.get('load') != 'HZ' or s.get('polarity') != 'NOR':
            raise BrokerError('range', 'Current AWG output/load/polarity is not commissioned')
        if any(s.get(k) != 'OFF' for k in ('burst', 'modulation', 'sweep')):
            raise BrokerError('busy', 'Current AWG must complete waveform cleanup first')
        # Dither is removed by a projected DC command at the SAME offset before
        # the bounded current ramp, not by disabling the physical AWG output.
        self.owner.validate_state(self.owner.hw().snapshot(), self.owner.policies())
        m=p['modulation']
        if s.get('waveform') not in ('DC','SINE','ARB') or s.get('amplitude_vpp',0.)/2 > min(m['max_step_v'],m['max_slew_v_per_s']*m['update_interval_s']):
            raise BrokerError('range','Dither removal exceeds the approved emission step budget')

    def apply(self, p, desired, permit):
        from ldcon.owners.ecdl_projection import constraints
        o, m = self.owner, p['modulation']
        hw = o.hw(); connection = hw.connections['current_awg']
        if o.laser['allow_output'] is not True:
            raise BrokerError('configuration','Laser output writes are not enabled')
        projected = constraints(o.device, 'current_awg')
        projected['writes_enabled']=o.laser['allow_output']
        connection.install_constraints(projected, preserve_cleanup=False)
        channel = o.device['connection']['resources']['current_awg']['channel']
        initial = self.before['outputs']['current']
        target = m['on_voltage_v'] if desired=='on' else m['off_voltage_v']
        step = min(m['max_step_v'], m['max_slew_v_per_s'] * m['update_interval_s'])
        count = max(1, math.ceil(abs(target-initial)/step))
        if count*m['update_interval_s'] > 600:
            raise BrokerError('range', 'Emission ramp exceeds the bounded 600-second trajectory budget')
        o.emission.check_cancelled()
        connection.set_dc(channel, initial)
        self.expected_voltage = initial
        dc=hw.snapshot()['current']
        self.verify_dc(dc,initial,p)
        last_write=time.time();end=time.monotonic()+600.
        previous_id=None
        for n in range(1, count+1):
            time.sleep(m['update_interval_s'])
            o.emission.check_cancelled()
            wait_until=min(end,time.monotonic()+p['verify_timeout_s'])
            while True:
                o.emission.check_cancelled()
                evidence = self.snapshot(p)
                o.emission.check_current(evidence['current'], p)
                sample=evidence['current']
                if sample['acquired_unix_s']>last_write and sample['acquisition_id']!=previous_id:
                    previous_id=sample['acquisition_id'];break
                if time.monotonic()>=wait_until:
                    raise BrokerError('measurement','No new current acquisition after the preceding emission step')
                time.sleep(p['sample_interval_s'])
            self.verify_preserved(dict(outputs=self.before['outputs'], reserved=self.reserved), evidence, desired)
            value = initial + (target-initial)*n/count
            o.emission.check_cancelled()
            hw.move('current', value)
            self.expected_voltage = value
            last_write=time.time()
            after=hw.snapshot()
            self.verify_dc(after['current'],value,p)
            actual = after['outputs']['current']
            if abs(actual-value) > m['readback_tolerance_v']:
                raise BrokerError('fault', 'Current modulation voltage did not read back')
        return time.time()

    @staticmethod
    def verify_dc(channel, voltage, p):
        if (channel.get('enabled') is not True or channel.get('load')!='HZ' or channel.get('polarity')!='NOR' or
                channel.get('waveform')!='DC' or any(channel.get(k)!='OFF' for k in ('burst','modulation','sweep')) or
                abs(finite(channel['value'],'current offset')-voltage)>p['modulation']['readback_tolerance_v']):
            raise BrokerError('fault','Current AWG did not verify the exact DC output state')

    def verify_preserved(self, before, after, desired):
        self.verify_dc(after['current_channel'],self.expected_voltage,self.policy)
        if not after['open_loop']:
            raise BrokerError('fault', 'PI/FF became active during emission transition')
        for axis in ('piezo','temperature'):
            tolerance = self.owner.device['control_settings']['axes'][axis]['readback_tolerance']
            if abs(before['outputs'][axis]-after['outputs'][axis]) > tolerance:
                raise BrokerError('fault', 'Emission transition changed preserved ' + axis)
        if before['reserved'] != after['reserved']:
            raise BrokerError('fault', 'Reserved AWG channel changed during emission transition')

    def close(self):
        from ldcon.owners.ecdl_projection import install_emission_inhibit
        state=self.owner.emission.store.status()
        if state['desired']=='off':
            install_emission_inhibit(self.owner,self.owner.hw())
        else:
            # Restoring normal projections requires the explicit verified ON
            # transition; the outer controller clears inhibition afterwards.
            from ldcon.owners.ecdl_projection import constraints
            for name,connection in self.owner.hw().connections.items():
                c=constraints(self.owner.device,name)
                c['writes_enabled']=self.owner.laser['allow_output']
                connection.install_constraints(c,preserve_cleanup=False)


def native_emission(system, state_file):
    p = system.policy.get('emission') or {'enabled': False}
    path = p.get('interlock_file', str(state_file) + '.emission.sqlite3')
    store = EmissionStore(path, system.authority.system_id)
    return EmissionController(store, lambda: system.policy.get('emission'), NativeEmissionAdapter(system))


def ecdl_emission(owner):
    store = EmissionStore(owner.audit_path.with_suffix('.emission.sqlite3'), owner.laser['id'])
    return EmissionController(store, lambda: owner.device['control_settings'].get('emission'), ECDLEmissionAdapter(owner))
