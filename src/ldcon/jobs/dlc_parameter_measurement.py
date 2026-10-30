"""Common change/measure adapter for one attached DLC parameter family."""
from copy import deepcopy
import math

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.dlc_pi_measurement import PIAdapter

FIELDS = {
    'pi': {'kp', 'ki_per_s', 'integral_limit_v'},
    'current': {'current_ma'},
    'slow_loop': {'tec_gain', 'tec_period_s', 'pzt_center_v'},
    'feedforward': {'ff_factor_ma_per_v'},
}


def candidate(family, value):
    if (family not in FIELDS or not isinstance(value, dict) or set(value) != FIELDS[family]
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value.values())):
        raise ValueError('Candidate must contain exactly the selected family fields as finite numbers')
    return deepcopy(value)


class ParameterAdapter(PIAdapter):
    def select_family(self, family):
        if self.token is not None:raise ValueError('Adjustment family is immutable after begin')
        capability = self.state.get('parameter_capabilities', {}).get(family, {})
        if capability.get('supported') is not True:
            raise ValueError('Owner does not support this adjustment: '+str(capability.get('reason', family)))
        self.family = family
        values = self.state.get('parameter_settings', {}).get(family)
        self.origin = candidate(family, values)

    def preflight(self, changes, mode, measurements, planned=None):
        if self.family == 'pi':return super().preflight(changes, mode, measurements, planned)
        self.check(force=True)
        if self.measurement_missing:raise MeasurementUnavailable('Await fresh frequency before parameter trial')
        if mode not in ('atomic', 'coordinated') or measurements.get('dither'):
            raise ValueError('DLC parameter comparison requires supported coordinated measurements')
        old = self.state['trial']['settings']
        values = deepcopy(old)
        for change in changes:
            key = change['parameter']
            if key not in FIELDS[self.family]:raise ValueError('Change belongs to another family')
            values[key] = change.get('value', old[key]+change.get('offset', 0.))
        candidate(self.family, values)
        return dict(candidate=values, checkpoint=deepcopy(old), revision=self.state['trial']['revision'],
                    mode=mode, binding='dlc_parameter', family=self.family, planned=values)

    def restore_checkpoint(self):
        """Restore commands, not optical state; the next frame waits separately."""
        self.check(force=True)
        self.uncertain = True
        try:
            receipt = self.session.rpc('pi_trial_reset', token=self.token,
                expected_revision=self.state['trial']['revision'])
        except BrokerError as error:
            if error.code in ('measurement', 'configuration', 'ownership', 'range'):
                self.uncertain = False
            raise
        if (receipt.get('confirmed') is not True or receipt.get('readback') != self.origin):
            raise DeviceError('Checkpoint command restoration was not confirmed')
        self.uncertain = False
        self.receipt = receipt
        return receipt

    def read(self, measurements):
        result = super().read(measurements)
        if measurements.get('optical_quality'):
            q = self.state.get('quality') or {}
            values = {k:v for k,v in (q.get('metrics') or {}).items()
                      if type(v) in (int, float) and math.isfinite(v)}
            result['optical_quality'] = dict(valid=q.get('fresh') is True and q.get('quality_valid') is True,
                acquired_unix_s=q.get('acquired_unix_s'), values=values,
                mode_id=q.get('mode_id'), single_mode=q.get('single_mode'))
        return result

    def complete_measurement(self, result, **kw):
        quality = [dict(phase=r['phase'], received_unix_s=r['received_unix_s'],
            measurements={'optical_quality':deepcopy(r['measurements']['optical_quality'])})
            for r in result['samples'] if 'optical_quality' in r['measurements']]
        super().complete_measurement(result, **kw)
        # Keep each source's acquisition timestamps. Optical frames have their
        # own cadence; do not repeat or interpolate them onto the fast WSU ring.
        result['samples'].extend(quality)
        result['samples'].sort(key=lambda r:min(b['acquired_unix_s'] for b in r['measurements'].values()))
        result['raw_source'] += '; optical frames retain independent source timestamps'
