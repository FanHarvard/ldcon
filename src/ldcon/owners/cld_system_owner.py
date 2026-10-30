"""CLD system adapter: activation, planning and typed operations under a projected laser policy (rule M step 3).

Before 2026-10-27 this module compiled the CLD operating limits from a laser document or from a copy in the owner's
``config.json`` and held them in a local ``SystemPolicyAuthority``. Now the policy is the projection that the laser's
system owner (sr1092: the native owner process, ``cld_projection.CLDProjectionClient``) installs on this owner for
CH0 with its policy revision. ``compile_policy`` never reads a laser document; without an installed projection the CLD
is read-only and has no operating bounds (rule I: no range in the projection, no write). The CLD current tuning
(``control_settings.current.pid``) is a layer-2 setting of the laser yaml: this owner no longer adopts it locally.
"""
from copy import deepcopy
import json

from experiment_core.errors import AuthorizationRequired, StaleContext
from experiment_core.records import digest, safe_id
from ldcon.drivers.cld1015 import Controller
from ldcon.drivers.cld1015_config import configuration
from ldcon.owners.owner_projection import Permit, VerifiedSystemSource

# Layer 1 (physical facts the maintainer keeps in config.json ``controller``). ``current_limits_a`` is treated as the
# diode's device rating (design Q1, open: if the human says it is an operating range it moves to the laser yaml).
LAYER1_CONTROLLER = frozenset(('current_limits_a', 'modulation_role', 'ld_enable_writes_enabled'))
# Layer-2 copies that older config files still carry: ignored at start (audited once), removed by ops/trim_owner_config.py.
LEGACY_CONTROLLER = frozenset(('temperature_limits_c', 'temperature_conditions', 'current_conditions', 'control_settings',
                               'current_writes_enabled', 'temperature_writes_enabled', 'temperature_conditions_file',
                               'policy_device'))
POLICY_KEYS = ('current_limits_a', 'temperature_limits_c', 'modulation_role', 'temperature_conditions', 'current_conditions',
               'control_settings', 'current_writes_enabled', 'temperature_writes_enabled', 'ld_enable_writes_enabled')


def derived_projection_source(system_id):
    """The projection binding a CLD derives from its system_id when config.json lacks ``projection_sources``: the
    site convention of rollout/stage_site.py (principal ``laser-<slug>``, key ``LDCON_PROJECTION_<SLUG>``; sr1092:
    system_id ``Sr1092`` -> ``laser-sr1092`` / ``LDCON_PROJECTION_SR1092``, like the SDG owners)."""
    slug = str(system_id).lower()
    return 'laser-' + slug, dict(system_id=system_id, key_env='LDCON_PROJECTION_' + slug.upper(), channels=[0])


def compile_policy(layer1, projection=None, fallback=None):
    """The CLD's effective policy: layer-1 device facts intersected with the projected layer-2 operating conditions.

    ``projection`` is the installed projection document (``ldcon.policy-projection/v1``: ``constraints`` from
    ``cld_projection.cld_constraints`` and the system's ``policy_revision``) or None. Never reads a laser document.
    Unprojected: read-only, device rating only, no conditions -- unless ``fallback`` (the legacy layer-2 copies of
    an untrimmed config.json) is given: then the pre-step-3 policy of those copies applies until a projection
    arrives (the owner audits ``layer1_copy_fallback``).
    """
    layer1 = {key: deepcopy(value) for key, value in (layer1 or {}).items() if key in LAYER1_CONTROLLER}
    if projection is None and fallback:
        return _legacy_policy(layer1, fallback)
    constraints = deepcopy(projection['constraints']) if projection else {}
    rating = layer1.get('current_limits_a')
    current = constraints.get('current_bounds_a')
    if rating is not None and current is not None:
        current = [max(rating[0], current[0]), min(rating[1], current[1])]
        if current[0] >= current[1]:
            raise ValueError('Conflicting CLD current envelopes (device rating vs projected operating bounds)')
    compiled = configuration(current_limits_a=current if current is not None else rating,
                             temperature_limits_c=constraints.get('temperature_bounds_c'),
                             modulation_role=layer1.get('modulation_role'),
                             temperature_conditions=constraints.get('temperature_conditions'),
                             current_conditions=constraints.get('current_conditions'),
                             control_settings=constraints.get('control_settings') or {},
                             current_writes_enabled=bool(constraints.get('current_writes_enabled', False)),
                             temperature_writes_enabled=bool(constraints.get('temperature_writes_enabled', False)),
                             ld_enable_writes_enabled=bool(layer1.get('ld_enable_writes_enabled', False)))
    result = json.loads(json.dumps({key: compiled[key] for key in POLICY_KEYS}, allow_nan=False))
    result.update(projected=projection is not None,
                  source_revision=projection['policy_revision'] if projection else None,
                  projection_revision=digest(projection) if projection else None)
    return result


def _legacy_policy(layer1, copies):
    """Pre-step-3 compilation from the config.json copies (fallback while no projection is installed)."""
    source = {**deepcopy(copies), **layer1}
    bounds = (source.get('current_conditions') or {}).get('bounds_a')
    rating = layer1.get('current_limits_a')
    if bounds is not None and rating is not None:
        source['current_limits_a'] = [max(rating[0], bounds[0]), min(rating[1], bounds[1])]
    elif bounds is not None:
        source['current_limits_a'] = bounds
    keys = {key for key in POLICY_KEYS if key in source}
    compiled = configuration(**{key: value for key, value in source.items() if key in keys})
    result = json.loads(json.dumps({key: compiled[key] for key in POLICY_KEYS}, allow_nan=False))
    result.update(projected=False, source_revision=None, projection_revision=None, fallback='layer1 copy')
    return result


class _NoSource:
    """Receipt verifier of a CLD that cannot verify a projection (no key in its environment): installs are refused."""
    def __init__(self, reason):
        self.reason = reason

    def __call__(self, principal, receipt, kind):
        raise AuthorizationRequired('This CLD cannot verify a projection: ' + self.reason)


class ProjectedPolicySource(VerifiedSystemSource):
    """The CLD owner's view of its laser system: a verified remote projection, never an editable policy."""
    def __init__(self, system_id, *, journal, verify_receipt, layer1, legacy=None):
        super().__init__(system_id, journal=journal, verify_receipt=verify_receipt)
        self.layer1 = deepcopy(layer1)
        self.legacy = deepcopy(legacy) or None

    def restore(self, document):
        """Re-read the last installed projection at start (design section 2.2): it serves reads and the bias-claim
        bounds; writes need a fresh install because the guard's generation changed with this process."""
        if isinstance(document, dict) and document.get('system_id') == self.system_id:
            self._state = deepcopy(document)

    @property
    def projected(self):
        return self._state is not None

    @property
    def source_revision(self):
        return self._state['policy_revision'] if self._state is not None else None

    def snapshot(self):
        self._current()
        return compile_policy(self.layer1, self._state, fallback=self.legacy)

    def permit(self, binding, operation, *, request_id, resources=None):
        """Locally initiated typed operations (a worker holding the controller lease) under the installed projection."""
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired('Binding belongs to another system')
        if self._state is None:
            raise AuthorizationRequired('No effective physical projection')
        active = binding._guard.inspect(binding)
        projection = active.get('projection') or {}
        if not active.get('projection_revision'):
            raise AuthorizationRequired('No effective physical projection')
        if (projection.get('policy_revision') != self.revision or projection.get('system_generation') != self.generation
                or projection.get('instrument_generation') != binding._guard.generation):
            raise StaleContext('Installed CLD projection differs from the verified system state; a fresh install is required')
        requested = binding.resources if resources is None else frozenset(resources)
        if not requested or not requested <= binding.resources:
            raise AuthorizationRequired('Operation expands its physical resource scope')
        return Permit(binding, active['projection_revision'], safe_id(request_id), digest(operation), requested)


class _SystemController(Controller):
    """Reuse established ramp/activation algorithms without owning a transport."""
    def __init__(self, system):
        self.system = system
        self.lock = system.owner.lock
        self.current_activation = None

    @property
    def config(self):
        return self.system.authority.snapshot()

    def read(self):
        return self.system.owner.instrument.io.read()

    def query(self, command):
        return self.system.owner.instrument.io.query(command)

    def command(self, command):
        physical = self.system.owner.physical_writer
        with physical.command(self.system.authority, self.system.binding, command):
            return self.system.owner.instrument.io.command(command)


class CLDLaserSystemOwner:
    def __init__(self, owner, layer1, system_id, journal, *, legacy=None):
        self.owner = owner
        self.layer1 = {key: deepcopy(value) for key, value in (layer1 or {}).items() if key in LAYER1_CONTROLLER}
        self.journal = journal
        self.notes = []            # (audit kind, values) for the owner to record once
        sources = owner.config.get('projection_sources') or {}
        if len(sources) > 1:
            raise ValueError('A CLD serves one laser system: configure at most one projection source')
        self.principal = next(iter(sources), None)
        specification = sources.get(self.principal)
        if self.principal is None and owner.config.get('system_id'):
            # No config edit needed before the first start: derive the site convention from the system id.
            self.principal, specification = derived_projection_source(owner.config['system_id'])
            self.notes.append(('projection_source_derived', dict(principal=self.principal, **specification)))
        self.key_env = specification['key_env'] if specification else None
        self.signed = False
        if specification is not None:
            from ldcon.owners.projection_transport import ReceiptVerifier, key_from_environment
            system_id = specification['system_id']
            try:
                key = key_from_environment(specification['key_env'])
            except AuthorizationRequired as exc:
                # The key is provisioned per laser in the site secrets env; without it the CLD keeps the pre-step-3
                # behaviour (legacy copies, no projection) and says so.
                self.verifier = _NoSource(str(exc))
                self.notes.append(('projection_key_absent', dict(principal=self.principal, key_env=specification['key_env'])))
            else:
                self.verifier = ReceiptVerifier(journal, self.principal, key, clock=owner.wall_clock)
                self.signed = True
        else:
            self.verifier = _NoSource('no system_id / projection_sources configured')
        self.authority = ProjectedPolicySource(system_id, journal=journal, verify_receipt=self.verifier,
                                               layer1=self.layer1, legacy=legacy)
        self.controller = _SystemController(self)
        self.binding = owner.physical_writer.guard.bind(self.authority)
        self.authority.restore(owner.physical_writer.guard.inspect(self.binding).get('projection'))

    # ---- projection RPC (the laser system owner is the only principal) --------------------------------------
    def projection_status(self, request):
        return self.owner.physical_writer.guard.inspect(self.binding)

    def projection_install(self, request):
        if self.principal is None or request.get('principal') != self.principal:
            raise AuthorizationRequired('No configured projection source covers this CLD')
        self.verifier.lease = request.get('lease')
        self.verifier.expected_revision = request.get('expected_revision')
        return self.authority.receive_projection(self.binding, request['principal'], request.get('receipt'),
                                                 expected_revision=request.get('expected_revision'))

    def sync_projection(self, policy=None):
        """Compatibility: the projection is installed by the system owner; nothing is compiled locally."""
        return self.owner.physical_writer.guard.inspect(self.binding)

    def release(self):
        self.controller.current_activation = None

    def describe(self):
        from ldcon.core.device_conditions import policy_digest
        policy = self.authority.snapshot()
        state = self.owner.physical_writer.guard.inspect(self.binding)
        projection = state.get('projection') or {}
        return dict(policy, policy_digest=policy_digest(policy['temperature_conditions']),
                    system_id=self.authority.system_id, source_revision=self.authority.source_revision,
                    policy_authority=dict(system_id=self.authority.system_id, principal=self.principal, key_env=self.key_env,
                                          signed=self.signed, fallback=policy.get('fallback'),
                                          projected=self.authority.projected, source_revision=self.authority.source_revision,
                                          projection_revision=state['projection_revision'],
                                          system_generation=projection.get('system_generation'),
                                          dependencies=deepcopy(projection.get('dependencies')),
                                          fault=state['fault']),
                    projection_revision=state['projection_revision'])

    def dispatch(self, request):
        method = request['method']
        controller, policy = self.controller, self.authority.snapshot()
        if method == 'describe':
            return self.describe()
        if method == 'read':
            return controller.read()
        if method == 'validate':
            return controller.validate(request['values'])
        if method == 'validate_sweep':
            return controller.validate_sweep(request['channels'], {}, request['resolved'])
        if not self.owner.write_binding_enabled:
            raise ValueError('CLD system binding is read-only')
        if not self.authority.projected:
            # The legacy-copy fallback serves reads and the bias bounds only; writes need the projection (rule I).
            raise ValueError('CLD has no installed system projection; writes are not granted (rule I)')
        gate=getattr(self.owner,'emission_interlock',None)
        authorized_emission=(gate is not None and method=='enable' and request.get('output')=='ld' and
                            gate.authorize_ld(request.get('emission_permit'),request.get('enabled')))
        if gate is not None and gate.status()['inhibited'] and not authorized_emission:
            raise ValueError('Intentional emission inhibit blocks ordinary CLD writes')
        if method == 'activate_current':
            from ldcon.owners.current_owner import activate
            return activate(controller, request['policy_digest'], request['settings_digest'])
        if method == 'apply':
            if 'current_a' in request['values'] and not policy['current_writes_enabled']:
                raise ValueError('CLD current writes are not enabled')
            return controller.apply(request['values'], ramp_s=request.get('ramp_s', 0))
        if method == 'enable':
            permitted=(policy.get('ld_enable_writes_enabled',False) if request.get('output')=='ld' and gate is not None else policy['temperature_writes_enabled'])
            if not permitted:
                raise ValueError('CLD output writes are not enabled')
            return controller.enable(request['output'], request['enabled'])
        if method == 'save_current_tuning':
            raise ValueError('CLD current tuning is a laser setting (control_settings.current of the laser yaml, rule M layer 2); '
                             'save it through the laser configuration and the system owner re-projects it')
        raise ValueError('Unsupported CLD system operation')
