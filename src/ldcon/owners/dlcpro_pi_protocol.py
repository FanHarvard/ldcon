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
        sample = o.read_control()
        try: quality = o._quality()
        except (OSError, MeasurementUnavailable): quality = {}
        return dict(mode=o.mode, fault=o.fault, controller_claimed=protocol.client is not None, target_frequency_hz=o.target,
            target_revision=o.target_revision, policy_revision=digest(o.policy),
            control=dict(getattr(o, 'control', {})), integral_v=o.integral,
            applied_pzt_v=o.applied_pzt if math.isfinite(o.applied_pzt) else None, last_tec_s=o.last_tec,
            frequency=asdict(sample), quality=quality, observed_s=o.clock(),
            trial=o.pi_trial.view())
    if action == 'pi_trial_samples':
        o.pi_trial.require(r['token'])
        if not hasattr(o.observation, 'samples_after'):
            raise ValueError('Acquisition history required for PI measurement')
        return dict(samples=[asdict(s) for s in o.observation.samples_after(r['after_s'])],
                    observed_s=o.clock())
    if not protocol.client: raise ValueError('A running controller lease is required')
    if action == 'pi_trial_begin':
        if r['device_revision'] != digest(protocol.device): raise ValueError('Laser definition changed')
        return o.pi_trial.begin(r['identity'], r['target_revision'], r['policy_revision'], r['watchdog_s'],r.get('target_program'))
    if action == 'pi_trial_heartbeat':
        o.pi_trial.require(r['token'])
        return o.pi_trial.view()
    if action == 'pi_trial_target_step':
        return o.pi_trial.step(r['token'],r['expected_revision'],r['index'],r['cycle'])
    if action == 'pi_trial_restore_target':
        return o.pi_trial.restore_target(r['token'])
    if action == 'pi_trial_apply':
        return o.pi_trial.apply(r['token'], r['expected_revision'], r['candidate'])
    if action == 'pi_trial_finish':
        if type(r['adopt']) is not bool: raise ValueError('Adoption must be boolean')
        return o.pi_trial.finish(r['token'], r['adopt'])
    raise ValueError('Unknown PI trial action')
