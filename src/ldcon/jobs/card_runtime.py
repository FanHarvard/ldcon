"""Per-card runtime schemas, with large static evidence kept out of requests."""
import copy

from ldcon.core.interface_schema import parameters
from ldcon.jobs.runtime_parameters import ParameterSpec, PrewriteRejected, RuntimeParameters
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError


class CardRuntime:
    def __init__(self, directory, card, p, emit, *, lock=None, laser=None):
        if card['operation']=='servo.ecdl_steady_lock':
            from ldcon.jobs.ecdl_steady import reject_retired
            reject_retired(p)
        self.directory=directory
        self.p, self.card, self.lock = p, card, lock
        self.emit = emit
        self.live = None
        if laser is not None:
            from ldcon.jobs.laser_live import LaserLive
            self.live = LaserLive(directory, laser, card, emit, lock=lock)
        operation = card['operation']
        specs = {}
        boundary = 'steady_cycle' if operation == 'servo.ecdl_steady_lock' else 'between_points'
        if operation == 'agent.monitor':
            boundary = 'between_reviews'
        def add(names, *, group=None, maximum=False, effect='memory'):
            for name in names:
                if name in p:
                    specs[name] = ParameterSpec(boundaries=(boundary,), group=group,
                        maximum=p[name] if maximum and type(p[name]) in (int, float) else None,
                        effect=effect)
        add(('sample_interval_s',))
        add(('max_sample_age_s','measurement_timeout_s','timeout_s'), maximum=True)
        if operation == 'agent.monitor':
            add(('interval_s','lookback_s','prompt','enabled','conditions','history_interval_s','history_min_new_samples','task_card_id','history_task_card_id','agent_card_id'))
            if operation=='agent.monitor':specs['enabled']=ParameterSpec(boundaries=(boundary,),while_busy=True)
        elif operation == 'study.ecdl_mode_map':
            from ldcon.jobs.ecdl_mode_map import runtime_specs
            specs=runtime_specs(p)
        elif operation == 'test.ecdl_recovery':
            from ldcon.jobs.ecdl_recovery_test import runtime_specs
            specs=runtime_specs(p)
        elif operation == 'prepare.ecdl_recovery':
            from ldcon.jobs.ecdl_recovery import runtime_specs
            specs=runtime_specs(p)
        elif operation == 'tune.ecdl_operating_point':
            add(('target_frequency_hz','target_wavelength_nm','tolerance_hz'), group='acquisition_target')
            # 2026-10-27 (plain FF approach): gain, thermal settle and the stability window apply from the next iteration.
            add(('approach_gain','stable_s','tec_settle_s'))
        elif operation == 'servo.ecdl_steady_lock':
            if lock is None:
                raise ValueError('Steady runtime requires its existing owner lease')
            if getattr(lock, 'runtime_owner_parameters', True) is False:
                p.update(lock.legacy_runtime_values)
                self.lock = None
                from ldcon.core.records import atomic_json
                atomic_json(directory/'runtime-capabilities.json', dict(owner_parameters_supported=False,
                    reason='既存ownerは版付きパラメーター変更に未対応です。周期・待機期限・実行時間だけ変更できます。',
                    unsupported_fields=['target_frequency_hz','tolerance_hz','strategy','tec_gain','tec_ff_paused','current_ff_enabled']))
            else:
                status = lock.parameters_status()
                self.owner_revision = status['revision']
                initial_patch={name:p[name] for name in ('tec_gain','tec_ff_paused','current_ff_enabled')
                               if p.get(name) is not None and p[name]!=status['parameters'][name]}
                if initial_patch:
                    status=lock.update_parameters(self.owner_revision,initial_patch)
                    self.owner_revision=status['revision']
                for name in ('tec_gain','tec_ff_paused','current_ff_enabled'):
                    p[name] = status['parameters'][name]
                # Recovery v2 invalidates the prior history on a target change;
                # the normal owner transaction and PI capture checks still apply.
                add(('target_frequency_hz','tolerance_hz'), group='native_target', effect='owner')
                add(('strategy','tec_ff_paused','current_ff_enabled'), effect='owner')
                add(('tec_gain',), maximum=True, effect='owner')
            add(('duration_s',))
            specs['window_guard'] = ParameterSpec(boundaries=(boundary,), while_busy=True,
                description='監視条件・次回中点探索の選択。一括更新。実行中の探索には適用しません。')
        else:
            raise ValueError('Card has no runtime parameter integration')
        self.hot = {name: copy.deepcopy(p[name]) for name in specs}
        self.specs = specs
        self.runtime = RuntimeParameters(self.hot, validator=self.validate, specs=specs,
            directory=directory, job_id=directory.name, card_id=card['id'], operation=operation,
            emit=emit, apply=self.apply if self.lock else None)

    @property
    def revision(self):
        return self.runtime.revision

    def validate(self, hot):
        candidate = {**self.p, **hot}
        normalized = parameters({**self.card,'parameters':{}},candidate)
        if self.card['operation'] == 'servo.ecdl_steady_lock' and normalized.get('window_guard',{}).get('card_id'):
            from ldcon.jobs.window_control import validate_selection
            from ldcon.core.interface_schema import load_mapping
            deck=load_mapping((self.directory/'interface-deck.yaml').read_text())
            validate_selection(deck,self.card['id'],normalized['window_guard'])
        if self.card['operation'] == 'tune.ecdl_operating_point':
            from ldcon.jobs.ecdl_operating_point import target
            if target(normalized) is None:
                raise ValueError('Alignment target cannot be cleared at runtime')
        return {key:normalized[key] for key in self.specs}

    def apply(self, candidate, changed):
        patch = {key:candidate[key] for key in changed if self.specs[key].effect=='owner'}
        if not patch:
            return
        # Groups must be complete at both the worker and owner transaction.
        if {'target_frequency_hz','tolerance_hz'} & patch.keys():
            patch.update({key:candidate[key] for key in ('target_frequency_hz','tolerance_hz')})
        try:
            result = self.lock.update_parameters(self.owner_revision,patch)
        except (MeasurementUnavailable,BrokerError) as error:
            if (not self.lock.uncertain_output and (isinstance(error,MeasurementUnavailable)
                    or error.code in ('range','configuration','ownership','measurement'))):
                raise PrewriteRejected(str(error)) from error
            raise
        self.owner_revision = result['revision']

    def poll(self, *, boundary, busy=False):
        if self.lock is not None:
            try:
                status = self.lock.parameters_status()
            except (OSError, MeasurementUnavailable, BrokerError) as error:
                if (self.lock.uncertain_output or isinstance(error, BrokerError)
                        and error.code not in ('io', 'measurement')):
                    raise
                # This RPC only reads owner memory. A transport dropout cannot
                # authorize an update, nor should it alone stop native PI.
                self.emit('runtime_parameters_status_unavailable', dict(reason=str(error)))
                busy = True
            else:
                busy = busy or bool(status.get('busy'))
                if status['revision'] != self.owner_revision:
                    values = {key: status['parameters'][key] for key, rule in self.specs.items()
                              if rule.effect == 'owner'}
                    self.runtime.observe_external(values, source='existing instrument owner',
                        source_revision=status['revision'], boundary=boundary)
                    self.owner_revision = status['revision']
                    self.p.update(copy.deepcopy(self.hot))
        result = self.runtime.poll(boundary=boundary,busy=busy)
        self.p.update(copy.deepcopy(self.hot))
        if self.live is not None:
            self.live.poll(boundary, busy=busy)
        if self.card['operation']=='servo.ecdl_steady_lock':
            from ldcon.core.records import atomic_json
            atomic_json(self.directory/'owner-runtime-context.json',dict(
                revision=getattr(self,'owner_revision',None),
                parameters={key:self.p[key] for key,rule in self.specs.items() if rule.effect=='owner'}))
        return result

    def close(self):
        self.runtime.close()


SUPPORTED = {'study.ecdl_mode_map','test.ecdl_recovery','prepare.ecdl_recovery','tune.ecdl_operating_point','agent.monitor'}
