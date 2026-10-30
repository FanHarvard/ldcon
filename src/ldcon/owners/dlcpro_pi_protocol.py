"""Scoped attached PI route, serialized by the normal owner protocol mutex."""
from dataclasses import asdict
from ldcon.core.interface_schema import digest
from ldcon.core.contracts import MeasurementUnavailable
import math


def dispatch(protocol, action, r):
    o = protocol.owner
    if action == 'pi_trial_receipt':
        return dict(trial=o.pi_trial.view(),target_frequency_hz=o.target,target_revision=o.target_revision,mode=o.mode,fault=o.fault)
    if action == 'pi_trial_state':
        from ldcon.owners.dlcpro_operating_mode import status as operating_mode_status
        from ldcon.owners.dlcpro_parameter_trial import settings as parameter_settings, capabilities
        sample = o.read_control()
        try: quality = o._quality()
        except (OSError, MeasurementUnavailable): quality = {}
        parameter_values = {}
        for family in ('current', 'slow_loop', 'feedforward'):
            try: parameter_values[family] = parameter_settings(o,family)
            except (ValueError, KeyError, TypeError, AttributeError): parameter_values[family] = None
        from ldcon.owners.dlcpro_pi_trial import KEYS
        control = getattr(o, 'control', {})
        parameter_values['pi'] = {key:control[key] for key in KEYS} if KEYS <= control.keys() else None
        parameter_capabilities = capabilities(o)
        parameter_capabilities['pi'] = dict(supported=parameter_values['pi'] is not None,
            reason=None if parameter_values['pi'] is not None else 'A started PI controller is required',
            fields=sorted(KEYS),units=dict(kp='1',ki_per_s='1/s',integral_limit_v='V'),
            adoption='runtime_only',optical_target_changes=False)
        return dict(mode=o.mode, fault=o.fault, controller_claimed=protocol.client is not None, target_frequency_hz=o.target,
            target_revision=o.target_revision, policy_revision=digest(o.policy),
            control=dict(getattr(o, 'control', {})), integral_v=o.integral,
            applied_pzt_v=o.applied_pzt if math.isfinite(o.applied_pzt) else None, last_tec_s=o.last_tec,
            frequency=asdict(sample), quality=quality, observed_s=o.clock(),
            trial=o.pi_trial.view(),operating_mode=operating_mode_status(o),
            parameter_settings=parameter_values,
            parameter_capabilities=parameter_capabilities)
    if action == 'pi_trial_samples':
        o.pi_trial.require(r['token'])
        if not hasattr(o.observation, 'samples_after'):
            raise ValueError('Acquisition history required for PI measurement')
        return dict(samples=[asdict(s) for s in o.observation.samples_after(r['after_s'])],
                    observed_s=o.clock())
    if not protocol.client: raise ValueError('A running controller lease is required')
    if action == 'pi_trial_begin':
        if r['device_revision'] != digest(protocol.device): raise ValueError('Laser definition changed')
        return o.pi_trial.begin(r['identity'], r['target_revision'], r['policy_revision'], r['watchdog_s'],r.get('target_program'),
            expected_operating_mode_revision=r.get('expected_operating_mode_revision'),monitor_grant=r.get('monitor_grant'),
            family=r.get('family','pi'))
    if action == 'pi_trial_heartbeat':
        o.pi_trial.require(r['token'])
        return o.pi_trial.view()
    if action == 'pi_trial_target_step':
        return o.pi_trial.step(r['token'],r['expected_revision'],r['index'],r['cycle'])
    if action == 'pi_trial_restore_target':
        return o.pi_trial.restore_target(r['token'])
    if action == 'pi_trial_apply':
        return o.pi_trial.apply(r['token'], r['expected_revision'], r['candidate'])
    if action == 'pi_trial_reset':
        return o.pi_trial.reset(r['token'], r['expected_revision'])
    if action == 'pi_trial_finish':
        if type(r['adopt']) is not bool: raise ValueError('Adoption must be boolean')
        return o.pi_trial.finish(r['token'], r['adopt'])
    raise ValueError('Unknown PI trial action')
