"""Native readback adapter for the shared bounded background PI manager."""
import copy

from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager, bounded_json
from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError


class NativeBackgroundPort:
    supports_validated_candidates = True
    def __init__(self, port, job_id):
        self.port, self.job_id = port, job_id
        self.manager = None

    @property
    def uncertain_output(self): return self.port.uncertain_output

    def response_info(self):
        return {'owner_context':{'system_id':self.background_status()['system_id']}}

    def continuation_info(self, original):
        state=self.port.read()  # Existing owner freshness/fault validation.
        bg=self.background_status()
        cfg=state['sample']['servo_config']
        return dict(context=dict(lease=self.port.lease,policy=state['policy_digest'],
            system_id=bg['system_id'],reference={k:cfg.get(k) for k in original['reference']}),
            native_pi_active=state.get('native_pi_active') is True and state.get('input_valid') is True,
            outputs={})

    def _effect(self, action, **values):
        if self.port.uncertain_output or not self.port.lease:
            raise DeviceError('Native background lease is unavailable or uncertain')
        self.port.uncertain_output = True
        try:
            result = self.port._call(action, lease=self.port.lease, **values)
        except BrokerError as exc:
            if exc.code in ('measurement','range','configuration','busy','request','ownership'):
                self.port.uncertain_output = False
                if exc.code == 'measurement': raise MeasurementUnavailable(str(exc)) from exc
                raise BrokerError('range',str(exc)) from exc
            raise
        self.port.uncertain_output = False
        return result

    def background_status(self): return self.port._pid_read('background_status')
    def background_reserve(self, ids): return self._effect('background_reserve', request_ids=ids)
    def background_release(self, ids): return self._effect('background_release', request_ids=ids)
    def background_restore(self, token, reason): return self._effect('background_restore', token=token, reason=reason)
    def background_abandon(self, reason): return self._effect('background_abandon', reason=reason)
    def background_pid_target(self, token, pid): return self._effect('background_pid_target', token=token, pid=pid)
    def background_pid_finish(self, token, status): return self._effect('background_pid_finish', token=token, status=status)

    def background_begin(self, **values):
        selection = bounded_json(self.manager.directory/'adaptive-experiment.json')
        values['identity'] = dict(values['identity'], controller_job_id=self.job_id,
                                 grant_revision=selection['background']['grant_revision'])
        return self._effect('background_begin', **values)


class NativeBackgroundManager(AdaptiveExperimentManager):
    def _select(self, busy):
        path = self.directory/'adaptive-experiment.json'
        if path.exists() and bounded_json(path).get('family') != 'pi':
            raise ValueError('Native background supports PI comparisons only')
        return super()._select(busy)

    def row(self, state, optical, error, tolerance):
        tuning = state.get('pid_tuning')
        if tuning:
            tuning = copy.deepcopy(tuning)
            raw = state['sample']['servo_status'].get('pid_integral', state['sample']['servo_status'].get('pid_integral_value'))
            limit = max(abs(x) for x in tuning['config']['raw_integral_bounds'])
            from ldcon.drivers.esp32_servo import finite_number
            tuning['eligible'] = finite_number(raw) and abs(raw)<.98*limit
            # Integral saturation is a tuning outcome, not a physical output
            # violation. Preserve it as evidence for owner-proved candidates.
            tuning['integral_observation'] = dict(
                valid=finite_number(raw) and limit>0 and abs(raw)<=limit*(1+1e-9),
                usage=abs(raw)/limit if finite_number(raw) and limit>0 else None,
                saturated=finite_number(raw) and limit>0 and abs(raw)>=.98*limit)
        return dict(pid_tuning=tuning, acquired_s=optical['acquired_s'], error_hz=error,
                    tolerance_hz=tolerance, outputs={'piezo':state['sample']['dac']['native_code']})


def manager(directory, port, laser, *, clock, emit, poll_interval_s):
    from pathlib import Path
    adapter = NativeBackgroundPort(port, Path(directory).name)
    result = NativeBackgroundManager(directory, adapter, laser_id=laser['id'],
                                    clock=clock, emit=emit, poll_interval_s=poll_interval_s)
    adapter.manager = result
    return result
