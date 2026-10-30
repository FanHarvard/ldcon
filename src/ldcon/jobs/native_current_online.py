"""Passive online comparison attached to an existing native-current lease."""
import copy

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.drivers.esp32_servo import finite_number


class NativePIDAdapter:
    def __init__(self, port):
        self.port = port
        self.state = port.pid_state()
        self.pid = copy.deepcopy(self.state['pid'])
        self.config = self.state['config']
        self.pending = None
        self.begun = False
        self.strategy = 'factor_reduction'

    def configure_strategy(self, strategy):
        if self.begun:
            raise ValueError('An existing native comparison must finish first')
        self.strategy = strategy

    def adaptive_candidate(self, pid, rows, minimum_effect_count):
        result = self.port.pid_check(pid)
        if not result['admissible']:
            return result
        cfg = self.state['config']
        raw_limit = max(abs(x) for x in cfg['raw_integral_bounds'])
        effects, required = [], []
        for _, row in rows:
            error, raw = row.get('wavelength_error_nm'), row.get('raw_integral')
            if not finite_number(error) or not finite_number(raw):
                return dict(admissible=False, reason='Native error/raw-I telemetry is insufficient to assess candidate effect')
            output = self.pid['kp']*error+self.pid['ki']*raw
            effects.append(abs((pid['kp']-self.pid['kp'])*error+(pid['ki']-self.pid['ki'])*raw))
            required.append(abs((output-pid['kp']*error)/pid['ki'])/raw_limit)
        effect = max(effects, default=0.)
        threshold = minimum_effect_count*self.config['resolution']
        result.update(predicted_effect_count=effect, required_raw_fraction=max(required, default=0.),
            effect_threshold_native_count=threshold,dac_quantum_native_count=self.config['resolution'],
            effect_basis='Reported native in-loop error/raw-I; measurability heuristic, not proof of zero influence')
        if effect < threshold:
            result.update(admissible=False, reason='Candidate effect is below the configured measurability threshold; not tested')
        elif result['required_raw_fraction'] > .9:
            result.update(admissible=False, reason='Candidate cannot retain observed correction with 10% raw-I reserve')
        return result

    def ready(self):
        return self.pending is None and self.state['ready']

    def retune(self, pid):
        # Intent only: the next fresh cycle performs the owner's bounded ramp.
        self.pending = copy.deepcopy(pid)
        self.pid = copy.deepcopy(pid)

    def advance(self):
        if self.pending is not None:
            if not self.begun:
                self.state = self.port.mutate('pid_begin', strategy=self.strategy)
                self.begun = True
            self.state = self.port.mutate('pid_target', pid=self.pending)
            self.pending = None
        if self.begun and not self.state['ready']:
            self.state = self.port.mutate('pid_step')

    def save_pid(self, pid):
        self.state = self.port.mutate('pid_save', pid=pid)
        self.config = self.state['config']
        return self.state

    def finish(self, status):
        if self.begun:
            self.state = self.port.mutate('pid_finish', status=status)
            self.begun = False
        self.config = self.state['config']

    def lock_ended(self):
        pass  # Main port close performs the verified fixed-reference PI stop.


class NativeCurrentOnline:
    def __init__(self, port, factory, clock):
        self.adapter = NativePIDAdapter(port)
        self.tuner = factory(self.adapter)
        self.clock = clock
        self.last_acquired = None
        self.gap_started = None

    def cycle(self, optical, sample, target, tolerance):
        acquired = optical['acquired_s']
        if self.last_acquired is not None and acquired-self.last_acquired > self.adapter.config['max_sample_gap_s']:
            self.tuner.restart_window('Comparison acquisition gap; reacquire a complete window')
        self.last_acquired = acquired
        self.gap_started = None
        self.gap_restarted = False
        self.adapter.advance()
        error = optical['frequency_hz']-target
        cfg = sample['servo_config']
        # Saturation prevents accepting an apparent gain improvement. Missing
        # raw-I telemetry also makes the comparison ineligible, never invented.
        raw = sample['servo_status'].get('pid_integral')
        if raw is None:
            raw = sample['servo_status'].get('pid_integral_value')
        limit = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
        limited = not finite_number(raw) or abs(raw) >= .98*limit
        self.tuner.tick(self.clock(), dict(error=error, filtered_error=error,
            output=sample['dac']['native_code'], limited=limited,
            acquired_s=acquired, wavelength_error_nm=sample['servo_status'].get('pid_error'),
            raw_integral=raw, integral_usage=abs(raw)/limit if finite_number(raw) else None,
            control_deadband=tolerance, requested_tolerance=tolerance), self.adapter, self.adapter)

    def wait(self, reason):
        now = self.clock()
        if self.gap_started is None:
            self.gap_started = now
        tuner = self.tuner
        if not tuner.active or tuner.finishing:
            return
        if tuner.cancel_request(tuner.active):
            tuner._finish('cancelled', self.adapter, 'Online tuning cancelled; native PI continues')
        elif now-tuner.started >= tuner.p['timeout_s']:
            tuner.invalidate('Comparison deadline; restoring original gains', self.adapter)
        elif now-self.gap_started > self.adapter.config['max_sample_gap_s']:
            if not getattr(self, 'gap_restarted', False):
                tuner.restart_window('Comparison gap: '+str(reason))
                self.gap_restarted = True

    def close(self):
        self.tuner.close(self.adapter)
