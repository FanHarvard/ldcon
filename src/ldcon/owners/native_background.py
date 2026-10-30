"""Owner-only, returnable native PI trials; no current, target or persistence API."""
import copy
import math
import os
from pathlib import Path
import time

from experiment_core.background import BackgroundCoordinator
from experiment_core.errors import Conflict, StaleContext
from experiment_core.records import safe_id
from ldcon.jobs.adaptive_window_session import bounded_json, finite
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_current_tuning import NativePITuning, pid_from
from ldcon.jobs.background_pid_session import background_parameters as parameters


class NativeBackground:
    def __init__(self, owner):
        self.owner = owner
        self.coordinator = BackgroundCoordinator(owner.clock)

    def system_id(self):
        return safe_id(os.environ.get('LDCON_BACKGROUND_LASER_ID') or self.owner.policy['optical']['expected_laser'])

    def status(self):
        cfg=self.owner.expected_config or {}
        actual=None
        if all(k in cfg for k in ('pid_kp','pid_ki','pid_integral_min','pid_integral_max')):
            actual=dict(kp_count_per_nm=cfg['pid_kp'],ki_count_per_nm_s=cfg['pid_ki'],
                        integral_limit_count=max(abs(cfg['pid_integral_min']),abs(cfg['pid_integral_max']))*abs(cfg['pid_ki']),
                        raw_integral_bounds=[cfg['pid_integral_min'],cfg['pid_integral_max']])
        return dict(self.coordinator.status(), actual_pid=actual, policy_revision=self.owner.definition,idle_foreground_fencing=True,
                    pi_strategies=['factor_reduction','validated_candidates'],
                    pi_candidate_fields=['kp_count_per_nm','ki_count_per_nm_s','integral_limit_count'],
                    integral_limit_kind='symmetric contribution in native DAC counts', owner_fault=self.owner.fault, system_id=self.system_id(),
                    supported_adjustments=['pi'], unsupported_adjustments=['current','slow_loop','feedforward'])

    def persist(self):
        self.owner._persist()

    def context(self):
        o = self.owner
        cfg = o.expected_config or {}
        return dict(lease=o.lease, policy=o.definition, system_id=self.system_id(),
                    reference={k:v for k,v in cfg.items() if k not in ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')})

    def grant(self, identity):
        """Read the host-owned grant on every effect; never trust model text."""
        if 'measurement_job_id' in identity:
            from ldcon.owners.background_grants import read_grant
            return read_grant(self.system_id(),identity,lease=self.owner.lease)
        root = Path(os.environ.get('LDCON_ADAPTIVE_ROOT', ''))
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            raise ValueError('Trusted adaptive root is required in native owner')
        laser = self.system_id()
        monitor = safe_id(identity['monitor_id']); job = safe_id(identity['controller_job_id'])
        directory = root/laser
        paths = [directory, directory/'monitors', directory/job,
                 directory/'monitors'/(monitor+'.json'), directory/job/'background-control.json']
        if any(p.is_symlink() for p in paths):
            raise ValueError('Background authority must not use symlinks')
        try:
            grant = bounded_json(paths[-2]); control = bounded_json(paths[-1])
        except OSError as exc:
            raise ValueError('Background authority is unreadable') from exc
        if (grant.get('enabled') is not True or grant.get('bounds',{}).get('enabled') is not True
                or control.get('enabled') is not True
                or grant.get('generation') != identity['generation']
                or control.get('generation') != identity['generation']
                or control.get('monitor_id') != monitor
                or grant.get('parameters_hash') != identity['grant_revision']
                or not finite(grant.get('last_seen')) or not finite(grant.get('max_age_s'))
                or not 0 < grant['max_age_s'] <= 180
                or not 0 <= time.time()-grant['last_seen'] <= grant['max_age_s']):
            raise ValueError('Background monitor/task grant is revoked or stale')
        return grant['bounds']

    def parent_stopped(self):
        bg = self.coordinator
        if bg.epoch:
            bg.last = dict(bg.epoch, state='parent_stopped', completed_at=self.owner.clock())
            bg.epoch = None
        bg.reservations.clear()
        bg.generation += 1
        self.persist()

    def boundary(self, action):
        bg = self.coordinator
        if bg.epoch:
            bg.reserve(['direct:'+action]); self.persist()
            raise BrokerError('busy', 'Background PI is returning before '+action)
        bg.generation+=1
        self.persist()

    def dispatch(self, request):
        bg, o = self.coordinator, self.owner
        action = request['action']
        if action == 'background_status': return self.status()
        try:
            if action == 'background_reserve':
                result = bg.reserve(request['request_ids']); self.persist(); return result
            if action == 'background_release':
                result = bg.release(request['request_ids']); self.persist(); return result
            if action == 'background_abandon':
                # Stop/recovery owns its own verified cleanup; never move back.
                if bg.epoch: bg.block('Parent ended: '+str(request.get('reason')))
                self.persist(); return self.status()
            if action == 'background_begin': return self.begin(request)
            epoch = bg.validate(request['token'], self.context(), restoring=action=='background_restore')
            if action == 'background_restore':
                bg.returning(request.get('reason','cancelled')); self.persist(); return self.status()
            self.grant(epoch['identity'])
            if action == 'background_pid_target':
                pid = request['pid']
                if epoch.get('strategy')=='validated_candidates':
                    state=o.pid_tuning.set_target(pid)
                    self.persist();return dict(self.status(),pid_tuning=self.decorate(state))
                if not any(all(math.isclose(pid[k], epoch['checkpoint']['pid_'+k]*factor,
                                           rel_tol=1e-9, abs_tol=1e-12) for k in ('kp','ki'))
                           and pid.get('kd') == 0 for factor in [1., *epoch['gain_factors']]):
                    raise ValueError('Gain target is outside the admitted factor set')
                state = o.pid_tuning.set_target(pid)
                self.persist(); return dict(self.status(), pid_tuning=self.decorate(state))
            if action == 'background_pid_finish':
                status = request['status']
                if status not in ('succeeded','unchanged','failed','cancelled'):
                    raise ValueError('Invalid background PI result')
                return self.complete(status)
            raise ValueError('Unsupported native background operation')
        except (ValueError, KeyError, TypeError, Conflict, StaleContext) as exc:
            raise BrokerError('range', str(exc)) from exc

    def begin(self, request):
        o, bg = self.owner, self.coordinator
        if (request.get('family') != 'pi' or not o.active or not o.prepared or o.pending or o.pid_tuning
                or o.retarget and o.retarget.get('active') or o.ff and o.ff.get('state') not in ('restored','fault')
                or o.system_owner.assemblies.active()):
            raise ValueError('Native background PI requires an idle, active clean lock')
        identity = request['identity']; bounds = self.grant(identity)
        p = parameters({'parameters':{}}, request['parameters'],require_candidate=True)
        approved = parameters({'parameters':{}}, bounds.get('pid_parameters',{}))
        fixed={k:v for k,v in p.items() if k!='candidate'}
        if ('pi' not in bounds.get('families',[]) or fixed != approved
                or p['strategy'] not in ('factor_reduction','validated_candidates')
                or p['save_settings'] or request['handoff_timeout_s'] != bounds['handoff_timeout_s']):
            raise ValueError('Native background PI differs from trusted monitor bounds')
        before = o._read_before(require_input=True)
        cfg = before['servo_config']
        if p['strategy']=='validated_candidates':
            from ldcon.owners.flexible_native_trial import FlexibleNativeTrial
            trial=FlexibleNativeTrial(o,before,p['candidate'],request['handoff_timeout_s'])
            steps=trial.maximum_return_steps;bound=trial.return_bound_s
        else:
            trial = NativePITuning(o, before)
            from ldcon.owners.native_current_tuning import effective_policy
            baseline = effective_policy(o.policy, o.saved_pid)['native']
            if not all(math.isclose(cfg['pid_'+key], baseline[field], rel_tol=1e-9, abs_tol=1e-12)
                       for key,field in [('kp','kp_count_per_nm'),('ki','ki_count_per_nm_s')]):
                raise ValueError('Background trial must start from configured gains; repeated reductions cannot compound')
            from ldcon.owners.esp32_native_owner import acceptance_bounds, C_NM
            errors = max(abs(x-cfg['target']) for x in acceptance_bounds(o.policy['native'], C_NM/cfg['target']))
            raw = max(abs(cfg['pid_integral_min']),abs(cfg['pid_integral_max']))
            steps = sum(math.ceil((1-min(p['gain_factors']))*abs(cfg[key])*sensitivity/.1)
                        for key,sensitivity in [('pid_kp',errors),('pid_ki',raw)])
            period = max(2*o.policy['max_sample_age_s'], o.policy['update_interval_s'])+o.interval_s
            bound = (steps+1)*period
            if bound > request['handoff_timeout_s']:
                raise ValueError('Native PI return needs %.6g s; handoff budget is %.6g s' % (bound,request['handoff_timeout_s']))
        bg.begin(request['expected_generation'], self.context(), copy.deepcopy(cfg),
                 request['handoff_timeout_s'], identity)
        bg.epoch.update(family='pi',strategy=p['strategy'],gain_factors=p['gain_factors'],return_bound_s=bound,
                        maximum_return_steps=steps,expires_at=o.clock()+p['timeout_s'],runtime_only=True)
        o.pid_tuning = trial
        self.persist()  # Durable checkpoint precedes the first coefficient effect.
        return dict(self.status(), started=True, pid_tuning=self.decorate(trial.state()))

    def decorate(self, state):
        result = copy.deepcopy(state)
        sample = (self.owner.state.get('sample') or {})
        raw = sample.get('servo_status',{}).get('pid_integral', sample.get('servo_status',{}).get('pid_integral_value'))
        bounds = result['config']['raw_integral_bounds']
        result['eligible'] = finite(raw) and abs(raw) < .98*max(abs(x) for x in bounds)
        return result

    def complete(self, status):
        o, bg = self.owner, self.coordinator
        epoch = bg.validate(bg.epoch['token'],self.context(),restoring=status!='succeeded')
        trial = o.pid_tuning
        state = trial.state()
        flexible=epoch.get('strategy')=='validated_candidates'
        from ldcon.owners.flexible_native_trial import close_pid,pid_from_config,FIELDS
        original=pid_from_config(epoch['checkpoint']) if flexible else pid_from(epoch['checkpoint'])
        expected = state['pid'] if status=='succeeded' else original
        if not state['ready'] or not close_pid(state['pid'],expected):
            raise BrokerError('busy','Native background PI restoration is incomplete')
        before = o._read_before(require_input=True)
        expected_cfg=trial.expected if flexible and status=='succeeded' else epoch['checkpoint']
        fields=FIELDS if flexible else ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')
        if not flexible and status=='succeeded':
            expected_cfg=dict(expected_cfg,**{'pid_'+k:v for k,v in expected.items()})
        if any(not math.isclose(before['servo_config'][k],expected_cfg[k],rel_tol=1e-8,abs_tol=1e-12) for k in fields):
            raise o._latch('Native background PI completion readback mismatch')
        trial.finish(status)
        bg.complete(epoch['token'],self.context(),dict(confirmed=True,response_ended=True,
            native_pi_active=o.active,actual_native=before['servo_config'],runtime_only=True),adopted=status=='succeeded')
        bg.release([x for x in bg.reservations if x.startswith('direct:')])
        self.persist()
        return dict(self.status(), pid_tuning=self.decorate(state))

    def poll(self):
        o, bg = self.owner, self.coordinator
        if not bg.epoch or bg.epoch['state']=='reconciliation_required': return
        try:
            epoch = bg.epoch
            if o.fault or not o.active or not o.pid_tuning:
                bg.block('Native owner fault, parent stop or lost trial'); self.persist(); return
            if epoch['state']=='active':
                try: self.grant(epoch['identity'])
                except (ValueError,OSError,KeyError,TypeError): bg.returning('grant_revoked')
                if o.clock() >= epoch['expires_at']: bg.returning('experiment_timeout')
            bg.validate(epoch['token'],self.context(),restoring=epoch['state']!='active')
            if epoch['state']=='returning':
                from ldcon.owners.flexible_native_trial import pid_from_config
                original=pid_from_config(epoch['checkpoint']) if epoch.get('strategy')=='validated_candidates' else pid_from(epoch['checkpoint'])
                o.pid_tuning.set_target(original)
            o.pid_tuning.step()
            if epoch['state']=='returning' and o.pid_tuning.state()['ready']:
                self.complete('cancelled')
        except BrokerError as exc:
            if exc.code != 'measurement': bg.block(str(exc)); self.persist()
        except Exception as exc:
            # No retry of unknown effects. The physical writer retains its fault.
            bg.block(str(exc)); self.persist()
