"""Native laser system authority over a projected ESP physical owner.

Legacy NativeCurrentOwner remains the supervisor adapter. Policy mutation,
approvals and projection compilation live here; the physical writer has no
laser document or approval-directory access.

Rule M steps 3-4 (2026-10-27): the operating policy (``control_settings.native_current``), the FF profile
(``native_ff_sweep``) and the CLD operating conditions (``devices.<ldc>``) come from the laser yaml (layer 2) at
start and change live through ``policy_update`` decisions; the owner's ``config.json`` keeps only the physical block.
The authority journal is the durable record of the EFFECTIVE policy: at start the yaml is adopted when the owner is
idle, otherwise it stays pending (``startup``) until a decision adopts it under the lease.
"""
from copy import deepcopy
import math
import os

from experiment_core.records import Journal, digest as content_digest
from ldcon.owners.owner_projection import SystemPolicyAuthority

PHYSICAL_KEYS = ('firmware_version', 'dac', 'adc', 'calibration')


def physical_definition(policy):
    return {key: deepcopy(policy[key]) for key in ('firmware_version', 'adc', 'dac')}


def physical_block(policy):
    """Layer 1 of the native owner config.json (rule M step 4): firmware, DAC, ADC and calibration."""
    return {key: deepcopy(policy[key]) for key in PHYSICAL_KEYS}


def physical_mismatch(physical, policy):
    """Keys of the config.json physical block that differ from the laser yaml (calibration.evidence is free text)."""
    result = []
    for key in PHYSICAL_KEYS:
        mine, theirs = deepcopy(physical.get(key)), deepcopy(policy.get(key))
        if key == 'calibration':
            for side in (mine, theirs):
                if isinstance(side, dict):
                    side.pop('evidence', None)
        if mine != theirs:
            result.append(key)
    return result


def assembly_wiring(laser):
    """The assembly dependency: the laser document without its layer-2 operating policy and target, so that policy
    edits (native_current, native_ff_sweep, CLD conditions) never invalidate the saved authority dependencies."""
    from ldcon.owners.cld_projection import CLD_TYPE
    wiring = deepcopy(laser)
    wiring.pop('target', None)
    for device in (wiring.get('devices') or {}).values():
        settings = device.get('control_settings')
        if isinstance(settings, dict):
            settings.pop('native_current', None)
            settings.pop('native_ff_sweep', None)
        if device.get('type') == CLD_TYPE:
            device.pop('absolute_conditions', None)
            device.pop('control_settings', None)
    return wiring


def saved_document(journal, scope, initial, dependencies):
    """The journal's saved document of a scope (inserting ``initial`` when the scope is new)."""
    from experiment_core.configuration import Configuration, Parameter
    configuration = Configuration(journal, scope, {'document': Parameter('safety', '', '', 'cycle')},
                                  {'document': deepcopy(initial)}, dependencies=deepcopy(dependencies))
    return configuration.state()


def migrate_dependencies(journal, scope, state, dependencies):
    """Upgrade a native authority journal written before step 4 (dependencies carried ``native_ff_profile`` and the
    digest of the whole assembly snapshot). Only when no adoption is pending and the physical definition is unchanged.
    Returns the migration record or None."""
    from experiment_core.configuration import Configuration, Parameter
    saved = state['dependencies']
    if saved == dependencies or state['pending'] is not None:
        return None
    legacy = {'physical_definition', 'native_ff_profile', 'assembly_definition'}
    if not set(saved) <= legacy or saved.get('physical_definition') != dependencies.get('physical_definition'):
        return None
    configuration = Configuration(journal, scope, {'document': Parameter('safety', '', '', 'cycle')},
                                  {'document': deepcopy(state['value']['document'])}, dependencies=deepcopy(saved))
    configuration.set_dependencies(dependencies)
    return dict(before=saved, after=dependencies)


def projected_code_bounds(policy, runtime):
    """Convert approved current/activation constraints to an inclusive code interval."""
    c, dac = policy['calibration'], policy['dac']
    slope = (dac['max_voltage'] - dac['min_voltage']) / 65535. * c['current_a_per_v']
    intercept = c['bias_current_a'] + (dac['min_voltage'] - c['zero_voltage_v']) * c['current_a_per_v']
    low, high = policy['current_bounds_a']
    activation = getattr(runtime, 'activation_a', None)
    if activation is not None:
        low, high = max(low, activation + policy['activation_range_a'][0]), min(high, activation + policy['activation_range_a'][1])
    def inverse(lo, hi, offset, gain):
        return sorted(((lo-offset)/gain, (hi-offset)/gain))
    a, b = inverse(low, high, intercept, slope)
    measured = getattr(runtime, 'activation_measured_a', None)
    code = getattr(runtime, 'activation_code', None)
    if measured is not None and code is not None:
        lo, hi = policy['current_bounds_a']
        lo, hi = max(lo, measured + policy['activation_range_a'][0]), min(hi, measured + policy['activation_range_a'][1])
        for factor in (1-c['gain_relative_uncertainty'], 1+c['gain_relative_uncertainty']):
            aa, bb = inverse(lo, hi, measured-code*slope*factor, slope*factor)
            a, b = max(a, aa), min(b, bb)
    return [max(0, math.ceil(a-1e-9)), min(65535, math.floor(b+1e-9))]


class NativeLaserSystemOwner:
    def __init__(self, runtime, policy, ff_profile, state_file, *, revision_function,
                 assembly_factory=None, assembly_definition_revision=None, laser=None, cld_link=None):
        from pathlib import Path
        from ldcon.owners.native_policy_reload import changes
        from ldcon.owners.native_policy_update import LIVE_ALLOWED
        self.runtime = runtime
        self._ff_profile = deepcopy(ff_profile)
        path = Path(str(state_file) + '.system.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(path)
        system_id = policy['optical']['expected_laser']
        dependencies = {'physical_definition': content_digest(physical_definition(policy))}
        assembly_definition_revision = assembly_definition_revision or getattr(assembly_factory, 'definition_revision', None)
        if assembly_definition_revision is not None:
            dependencies['assembly_definition'] = assembly_definition_revision
        scope = system_id + '.policy'
        state = saved_document(self.journal, scope, policy, dependencies)
        saved = state['value']['document']
        diff = sorted(changes(saved, policy))
        outside = [p for p in diff if p not in LIVE_ALLOWED]
        if outside:
            raise ValueError('Laser native policy differs from the saved effective policy outside its live-adoptable fields '
                             '(physical/wiring need a reviewed restart): ' + ', '.join(outside))
        self.startup = dict(diff=diff, pending=bool(diff), state='pending' if diff else 'equal',
                            requested_revision=revision_function(policy), migrated=migrate_dependencies(self.journal, scope, state, dependencies))
        self._requested_policy = deepcopy(policy)
        self.authority = SystemPolicyAuthority(system_id, saved,
            dependencies=dependencies,
            journal=self.journal, revision_function=revision_function, classify_change=self.classify_change)
        self.instrument = None
        from ldcon.owners.native_assembly_host import NativeAssemblyHost
        self.assemblies = NativeAssemblyHost(self, assembly_factory)
        from ldcon.owners.emission_adapters import native_emission
        self.emission = native_emission(self, state_file)
        from ldcon.owners.operating_mode_owner import OperatingModeOwner
        mode_system_id = os.environ.get('LDCON_BACKGROUND_LASER_ID') or system_id
        self.operating_mode = OperatingModeOwner(self.journal, mode_system_id,
            coordinator=lambda: getattr(getattr(self.runtime, 'background', None), 'coordinator', None),
            interrupt=self._interrupt_background_mode, persist=lambda: self.runtime._persist())
        # Rule M step 3: the laser's CLD operating conditions are this system's second policy document, projected
        # to the independent CLD owner (CH0) with a revision; never a copy in the CLD's config.json.
        self.cld_authority = self.cld = None
        self.cld_startup = dict(state='absent', pending=False, diff=[])
        self.cld_checked = None
        self.cld_check_interval_s = 30.
        if laser is not None:
            self._bind_cld(laser, revision_function, cld_link, Path(str(state_file) + '.cld.sqlite3'))

    def _bind_cld(self, laser, revision_function, link, journal_path):
        from ldcon.owners.cld_projection import CLDProjectionClient, cld_device, cld_document
        from ldcon.owners.native_policy_reload import changes
        found = cld_device(laser)
        if found is None:
            return
        key, device = found
        requested = cld_document(laser, key)
        scope = self.authority.system_id + '.cld'
        dependencies = {'cld_connection': content_digest(device['connection'])}
        # Its own journal: the same system_id as the native authority (the CLD verifies the laser's system_id) in a
        # separate generation table; the receipt sequences of this sender are per receiver anyway.
        self.cld_journal = Journal(journal_path)
        state = saved_document(self.cld_journal, scope, requested, dependencies)
        saved = state['value']['document']
        if state['dependencies'] != dependencies:
            raise ValueError('CLD wiring (connection) changed; reconcile the CLD authority journal before startup')
        diff = sorted(changes(saved, requested))
        self.cld_startup = dict(diff=diff, pending=bool(diff), state='pending' if diff else 'equal',
                                requested_revision=revision_function(requested))
        self._requested_cld = deepcopy(requested)
        self.cld_authority = SystemPolicyAuthority(self.authority.system_id, saved, dependencies=dependencies,
                                                   journal=self.cld_journal, scope=scope, revision_function=revision_function)
        self.cld = CLDProjectionClient(self.cld_authority, laser, key, journal=self.cld_journal, link=link)

    def _interrupt_background_mode(self, reason):
        bg = self.runtime.background.coordinator
        bg.generation += 1
        bg.returning(reason)

    @staticmethod
    def classify_change(before, after):
        from ldcon.owners.native_policy_reload import changes
        from experiment_core.configuration import Parameter
        # Free (operational) native-policy leaves: metadata, the anti-windup allocation and, since the target-ff design
        # (human decisions 2026-10-27), the per-step target cap, the FF scale and the post-step recentre fraction.
        metadata = {'revision', 'calibration.evidence', 'native.evidence', 'native.integral_limit_count',
                    'native.target_ramp_step_hz', 'native.target_feedforward_scale', 'native.recentre_integral_fraction'}
        return Parameter('operational' if set(changes(before, after)) <= metadata else 'safety', '', '', 'cycle')

    @property
    def policy(self):
        return self.authority.snapshot()

    @property
    def ff_profile(self):
        return deepcopy(self._ff_profile)

    def set_ff_profile(self, profile):
        """Adopt a validated FF profile (policy_update with the validate_profile proof, or the yaml at an idle start)."""
        from ldcon.owners.native_ff_proof import validate_profile
        self._ff_profile = deepcopy(validate_profile(profile, self.policy)) if profile is not None else None

    # ---- startup adoption of the laser yaml (rule M step 4) ----------------------------------------------------
    def startup_receipt(self, kind, diff, revision):
        return dict(human_identity=dict(source='laser definition adopted at owner start', kind=kind, changed_paths=list(diff)),
                    receipt_id='startup:' + kind + ':' + revision)

    def adopt_startup(self, *, idle):
        """Adopt the yaml's native policy at start. Idle (no lease, PI off, no fault/intent): adopt now (the yaml is the
        approved layer-2 document). Otherwise keep the saved effective policy and report the yaml as pending; the
        dashboard's policy_update adopts it under the lease with its decision and hardware re-check."""
        s = self.startup
        if not s['pending']:
            return s['state']
        if not idle:
            s['state'] = 'pending'
            return 'pending'
        from ldcon.owners.native_policy_reload import check_activation_range
        requested = deepcopy(self._requested_policy)
        check_activation_range(requested)
        interval = getattr(self.runtime, 'interval_s', None)
        if interval is not None and interval > requested['max_sample_age_s']:
            raise ValueError('Owner poll interval exceeds the native freshness budget of the laser definition')
        self.adopt(requested, receipt=self.startup_receipt('native_current', s['diff'], s['requested_revision']),
                   expected_revision=self.authority.revision,
                   prepare=lambda candidate: dict(startup=True, changed_paths=list(s['diff'])), commit=lambda policy, proof: None)
        s.update(state='adopted', pending=False)
        return 'adopted'

    def adopt_cld_startup(self, *, idle):
        s = self.cld_startup
        if self.cld_authority is None or not s['pending']:
            return s['state']
        if not idle:
            s['state'] = 'pending'
            return 'pending'
        from ldcon.owners.cld_projection import cld_constraints
        requested = deepcopy(self._requested_cld)
        cld_constraints(requested)
        self.adopt_cld(requested, receipt=self.startup_receipt('cld', s['diff'], s['requested_revision']),
                       expected_revision=self.cld_authority.revision)
        s.update(state='adopted', pending=False)
        return 'adopted'

    def adopt_cld(self, candidate, *, receipt, expected_revision):
        """Adopt a CLD policy document: approve, install the projection on the CLD owner, verify its readback."""
        from ldcon.owners.cld_projection import cld_constraints
        authority, client = self.cld_authority, self.cld
        token = (authority.approve(candidate, expected_revision=expected_revision,
                                   authorize_human=lambda proposal: deepcopy(receipt)) if receipt is not None else None)
        def apply(policy, proof):
            client.install(policy)
        def verify(policy):
            evidence = client.verify(policy)
            evidence['settled'] = bool(evidence['settled']) and evidence['source_revision'] == authority._revision(policy)
            return evidence
        return authority.adopt(candidate, expected_revision=expected_revision, approval=token,
                               prepare=lambda policy: {'constraints': cld_constraints(policy)},
                               apply=apply, verify=verify, operational_authorized=receipt is None)

    def cld_tick(self, now, *, force=False):
        """Keep the CLD projection installed (CLD restart, pending reconciliation); periodic, never fatal."""
        if self.cld is None:
            return None
        if not force and self.cld_checked is not None and now - self.cld_checked < self.cld_check_interval_s:
            return None
        self.cld_checked = now
        if self.cld_authority.configuration.state()['pending'] is not None:
            try:
                self.cld_authority.reconcile(self.cld.verify)
            except Exception as exc:       # readback not settled yet; retried at the next tick
                self.cld.last_error = 'reconcile: %s' % exc
                return False
        return self.cld.ensure()

    def constraints(self, policy=None):
        from ldcon.owners.esp32_native_owner import PRESERVED, CONTROLLED
        from ldcon.owners.native_modulation_limits import report
        policy = self.policy if policy is None else policy
        return dict(code_bounds=projected_code_bounds(policy, self.runtime),
            allowed_settings=sorted(set(PRESERVED + CONTROLLED + ('pid_reset_state', 'log_adc_change_threshold'))),
            cleanup_operations=[dict(kind='native_control', values={'pid_multiplier': 0})],
            activation_lease=getattr(self.runtime, 'lease', None),
            **report(policy),
            autonomous_step_slew_limited=False)

    def sync_projection(self, policy=None):
        return self.instrument.install(self.constraints(policy))

    def policy_state(self):
        transaction = self.authority.configuration.state()
        pending = transaction['pending']
        desired = pending['proposal']['changes']['document'] if pending else self.policy
        physical = self.instrument.guard.inspect(self.instrument.binding)
        startup = {k: v for k, v in self.startup.items() if k != 'migrated'}
        cld = None
        if self.cld_authority is not None:
            cld = dict(effective_revision=self.cld_authority.revision, pending=self.cld_authority.configuration.state()['pending'] is not None,
                       startup=deepcopy(self.cld_startup), projection=self.cld.describe())
        return dict(system_id=self.authority.system_id, generation=self.authority.generation,
                    desired_revision=self.authority._revision(desired), effective_revision=self.authority.revision,
                    saved_revision=self.startup.get('requested_revision'), startup=startup,
                    dependencies=self.authority.dependencies, pending=deepcopy(pending),
                    projection_revision=physical['projection_revision'], projection_fault=physical['fault'],
                    ff_profile_digest=self.runtime.ff_definition, cld=cld,
                    actual_settings=deepcopy((self.runtime.state.get('sample') or {}).get('servo_config')))

    def dispatch(self, request):
        from ldcon.owners.operating_mode_owner import MODE_ACTIONS
        with self.emission.store.cleanup_scope(request.get('action')):
            action = request.get('action')
            if action in MODE_ACTIONS:
                if request.get('device_id') != self.instrument.device_id:
                    raise ValueError('Operating-mode device identity mismatch')
                if action == 'operating_mode_status':
                    return self.operating_mode.dispatch(request)
                with self.runtime.io_lock:
                    return self.operating_mode.dispatch(request)
            if isinstance(action, str) and action.startswith('background_'):
                with self.runtime.io_lock:
                    self.operating_mode.before(request)
                    result = self._dispatch(request)
                    return self.operating_mode.after(request, result)
            return self.operating_mode.after(request, self._dispatch(request))

    def _dispatch(self, request):
        if request.get('action') in ('emission_status','emission_cancel'):
            if request.get('device_id') != self.instrument.device_id:
                raise ValueError('Emission device identity mismatch')
            return self.emission.dispatch(request)
        with self.instrument.authorized(self.authority):
            action = request.get('action')
            if isinstance(action, str) and action.startswith('emission_'):
                if request.get('device_id') != self.instrument.device_id:
                    raise ValueError('Emission device identity mismatch')
                with self.runtime.io_lock:
                    return self.emission.dispatch(request)
            self.emission.store.require_control(action)
            if isinstance(action, str) and action.startswith('assembly_'):
                if action == 'assembly_start':
                    with self.runtime.io_lock:
                        self.emission.store.require_control(action)
                        if not self.runtime.lease or request.get('lease') != self.runtime.lease:
                            from ldcon.core.instrument_client import BrokerError
                            raise BrokerError('ownership', 'Exact native device lease required')
                        self.runtime.background.boundary(action)
                        return self.assemblies.dispatch(request)
                with self.runtime.io_lock:
                    self.emission.store.require_control(action)
                    return self.assemblies.dispatch(request)
            if action in ('policy_update', 'policy_update_prepare'):
                return self.policy_update(request)
            if action == 'policy_reload':
                return self.policy_reload(request)
            if action in ('stop', 'release'):
                with self.assemblies.parent_boundary(request):
                    result = self.runtime._dispatch_system(request)
                    if action == 'release':
                        self.sync_projection()
                    return result
            result = self.runtime._dispatch_system(request)
            if request.get('action') in ('claim', 'release'):
                self.sync_projection()
            return result

    def poll(self):
        with self.runtime.io_lock, self.instrument.authorized(self.authority):
            self.operating_mode.reconcile()
            return self.runtime._poll_system()

    def policy_update(self, request):
        from ldcon.owners.native_policy_update import _update
        with self.runtime.io_lock:
            self._adoption_boundary(read_only=request.get('action') == 'policy_update_prepare')
            return _update(self.runtime, request)

    def policy_reload(self, request):
        # TODO (design Q3, open): the idle-only protected-artifact path is a candidate for removal once the human
        # confirms; its module also hosts the shared durable read/write/journal helpers, so it stays for now.
        from ldcon.owners.native_policy_reload import _reload
        with self.runtime.io_lock:
            self._adoption_boundary()
            return _reload(self.runtime, request)

    def _adoption_boundary(self, *, read_only=False):
        self.emission.store.require_control('policy_adoption')
        if read_only:
            if self.runtime.background.coordinator.epoch:
                from ldcon.core.instrument_client import BrokerError
                raise BrokerError('busy', 'Finish the active background trajectory before policy preparation')
        else:
            self.runtime.background.boundary('policy_adoption')
        if self.assemblies.active():
            from ldcon.core.instrument_client import BrokerError
            raise BrokerError('busy', 'Assembly cleanup/inspection must finish before policy adoption')

    def adopt(self, candidate, *, receipt, prepare, commit, expected_revision):
        """Existing exact approval validators call this only after validation."""
        token = (self.authority.approve(candidate, expected_revision=expected_revision,
                                       authorize_human=lambda proposal: deepcopy(receipt)) if receipt is not None else None)
        def apply(policy, proof):
            commit(policy, proof)
            self.sync_projection(policy)
        def verify(policy):
            state = self.instrument.guard.inspect(self.instrument.binding)
            source = self.authority._revision(policy)
            return dict(settled=state['fault'] is None and state['projection']['policy_revision'] == source,
                        source_revision=source, projections={self.instrument.device_id: state['projection_revision']})
        return self.authority.adopt(candidate, expected_revision=expected_revision, approval=token,
                                    prepare=prepare, apply=apply, verify=verify, operational_authorized=receipt is None)
