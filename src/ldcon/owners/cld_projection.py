"""The laser system owner's CLD policy and its signed CH0 projection to the independent CLD owner (rule M step 3).

Layer 2 (the laser yaml ``devices.<ldc>.absolute_conditions`` / ``control_settings``) is held by the laser's system
owner (for sr1092: the native owner process, ``NativeLaserSystemOwner``) as the document of a second
``SystemPolicyAuthority`` (scope ``<system_id>.cld``). The CLD physical owner receives it only as a projection
(``projection_install`` RPC, HMAC receipt of ``projection_transport.sign``) carrying the system's policy revision,
generation and dependencies, exactly like the SDG channel projections of the ECDL rig. The CLD owner never reads a
laser document; its ``config.json`` keeps only physical facts (``cld_owner.LAYER1_CONTROLLER``).
"""
from copy import deepcopy
import time

from experiment_core.errors import StaleContext
from experiment_core.records import digest, safe_id
from ldcon.core.instrument_client import BrokerError, BrokerTransport, validate_endpoint

CLD_TYPE = 'thorlabs.cld1015.broker'
# The laser-document paths (relative to ``devices.<ldc>``) that a policy_update decision may change live.
CLD_LIVE_PREFIXES = ('absolute_conditions.current_a.', 'absolute_conditions.temperature_c.', 'control_settings.')


def cld_device(laser):
    """The one CLD1015 device of a laser document as ``(key, device)``, or None."""
    found = [(key, device) for key, device in (laser.get('devices') or {}).items() if device.get('type') == CLD_TYPE]
    if not found:
        return None
    if len(found) > 1:
        raise ValueError('A laser binds at most one CLD1015 device')
    return found[0]


def cld_document(laser, key):
    """Layer-2 operating conditions of the CLD as the system owner's policy document (1:1 with the laser yaml)."""
    device = laser['devices'][key]
    return dict(device=key, allow_output=bool(laser['allow_output']),
                absolute_conditions=deepcopy(device.get('absolute_conditions') or {}),
                control_settings=deepcopy(device.get('control_settings') or {}))


def cld_constraints(document):
    """What the CLD physical owner receives: conditions, settings, bounds and write grants (validated)."""
    conditions = document.get('absolute_conditions') or {}
    temperature = conditions.get('temperature_c')
    current = conditions.get('current_a') or {}
    settings = deepcopy(document.get('control_settings') or {})
    allow = bool(document.get('allow_output'))
    current_enabled = allow and bool((settings.get('current') or {}).get('enabled', False))
    result = dict(temperature_conditions=deepcopy(temperature),
                  current_conditions=deepcopy(current) if 'max_step_a' in current else None,
                  control_settings=settings,
                  current_bounds_a=deepcopy(current.get('bounds_a')),
                  temperature_bounds_c=deepcopy((temperature or {}).get('bounds_c')),
                  current_writes_enabled=current_enabled, temperature_writes_enabled=allow,
                  enable_writes_enabled=allow)
    validate_cld_constraints(result)
    return result


def validate_cld_constraints(constraints):
    from ldcon.core.current_settings import CurrentConditions
    from ldcon.core.device_conditions import TemperatureConditions
    for key in ('current_bounds_a', 'temperature_bounds_c'):
        bounds = constraints.get(key)
        if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 2
                                   or any(type(v) not in (int, float) for v in bounds) or not bounds[0] < bounds[1]):
            raise ValueError('CLD ' + key + ' must be an increasing numeric pair')
    if constraints.get('current_conditions') is not None:
        CurrentConditions(constraints['current_conditions'], constraints['control_settings'])
    temperature = constraints.get('temperature_conditions')
    if temperature is not None and all(temperature.get(k) is not None for k in ('bounds_c', 'max_slew_c_per_s', 'max_step_c')):
        TemperatureConditions(temperature, constraints['control_settings'])
    return constraints


def projection_specification(laser, key):
    """The signing binding (principal, key_env) the system uses for this CLD: the CLD's own ``connection.projection``
    or, failing that, the laser's AWG projection binding (one principal per laser)."""
    device = laser['devices'][key]
    candidates = [device.get('connection', {}).get('projection')]
    candidates += [other.get('connection', {}).get('projection') for other in laser['devices'].values()]
    for spec in candidates:
        if isinstance(spec, dict) and set(spec) == {'principal', 'key_env'}:
            return dict(spec)
    if laser.get('id'):
        slug = str(laser['id']).lower()        # the site convention (rollout/stage_site.py, cld_system_owner.derived_projection_source)
        return dict(principal='laser-' + slug, key_env='LDCON_PROJECTION_' + slug.upper())
    return None


class _Link:
    """Loopback owner RPC with the broker reply shape (same transport as BiasLease)."""
    _rpc = BrokerTransport._rpc

    def __init__(self, endpoint, instrument, timeout_s=5.):
        self.endpoint = validate_endpoint(endpoint)
        self.instrument = instrument
        self.timeout_s = timeout_s

    def rpc(self, action, **values):
        return self._rpc(action, instrument=self.instrument, **values)


class CLDProjectionClient:
    """Installs the system's CLD policy as a signed CH0 projection on the independent CLD owner.

    Mirrors ``projection_transport.ProjectionClient`` (SDG channels): the document carries the authority's policy
    revision, generation and dependencies; the CLD owner verifies the receipt against its ``projection_sources``
    entry for this principal and installs it only against the expected current projection revision. No lease is
    involved: the projection precedes the native bias lease (the CLD checks its bias bounds against it).
    """
    def __init__(self, authority, laser, key, *, journal, link=None, clock=time.time):
        device = laser['devices'][key]
        connection = device['connection']
        self.authority, self.journal, self.clock = authority, journal, clock
        self.device_key = key
        self.endpoint = connection['endpoint']
        self.instrument = connection['instrument']
        self.resource = connection['resource']
        spec = projection_specification(laser, key)
        if spec is None:
            raise ValueError('The laser definition carries no projection signing binding (connection.projection) for its CLD')
        self.principal = safe_id(spec['principal'])
        self.key_env = spec['key_env']
        self.link = link if link is not None else _Link(self.endpoint, self.instrument)
        self.state = None
        self.last_error = None
        self.installed_unix_s = None
        self.checked = None

    def status(self):
        return self.link.rpc('projection_status', principal=self.principal)

    def document(self, status, policy=None):
        authority = self.authority
        authority._current()
        constraints = cld_constraints(policy if policy is not None else authority.snapshot())
        return dict(schema='ldcon.policy-projection/v1', system_id=authority.system_id,
                    system_generation=authority.generation, policy_revision=authority._projection_revision,
                    instrument_id=status['instrument_id'], instrument_generation=status['generation'],
                    physical_definition_revision=status['definition_revision'],
                    resources=[self.resource], dependencies=authority.dependencies, constraints=constraints)

    def install(self, policy=None):
        """Install the authority's (or the candidate's) CLD policy; a no-op when the CLD already holds it."""
        from ldcon.owners.projection_transport import key_from_environment, sign
        key = key_from_environment(self.key_env)
        status = self.status()
        document = self.document(status, policy)
        if status.get('projection') == document and not status.get('fault'):
            self.state = status
            return status
        receipt = sign(self.journal, self.principal, key, 'projection', document, lease=None,
                       expected_revision=status.get('projection_revision'), clock=self.clock)
        adopted = self.link.rpc('projection_install', principal=self.principal,
                                expected_revision=status.get('projection_revision'), receipt=receipt)
        if adopted.get('projection') != document or adopted.get('projection_revision') != digest(document):
            raise StaleContext('CLD owner did not verify the exact projected policy')
        self.state = adopted
        self.installed_unix_s = self.clock()
        self.last_error = None
        return adopted

    def ensure(self):
        """Install when the CLD lacks the effective projection (CLD restart, generation change). Returns True on
        success; a transport/authorization failure is recorded (``last_error``) and reported, never raised."""
        try:
            self.install()
            self.checked = self.clock()
            return True
        except Exception as exc:              # the next poll/claim retries; nothing was written to the CLD output
            self.last_error = '%s: %s' % (type(exc).__name__, exc)
            self.checked = self.clock()
            return False

    def require(self):
        """Claim-time guarantee: the CLD must hold the effective projection before the bias lease is taken."""
        if not self.ensure():
            raise BrokerError('configuration', 'CLD projection is not installed: ' + str(self.last_error))
        return self.state

    def verify(self, policy):
        """Settled readback of the CLD projection for the authority's adopt/reconcile."""
        status = self.status()
        projection = status.get('projection') or {}
        return dict(settled=not status.get('fault') and bool(projection), source_revision=projection.get('policy_revision'),
                    projections={self.instrument: status.get('projection_revision')})

    def describe(self):
        return dict(principal=self.principal, key_env=self.key_env, endpoint=self.endpoint, instrument=self.instrument, resource=self.resource,
                    device=self.device_key, installed_unix_s=self.installed_unix_s, checked_unix_s=self.checked,
                    last_error=self.last_error,
                    projection_revision=(self.state or {}).get('projection_revision'),
                    projected_policy_revision=((self.state or {}).get('projection') or {}).get('policy_revision'),
                    effective_policy_revision=self.authority.revision)
