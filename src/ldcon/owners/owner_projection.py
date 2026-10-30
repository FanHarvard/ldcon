"""Trusted local system authority and mandatory physical projection boundary.

Bindings and permits are host objects, not RPC credentials. Transport adapters
must resolve an authenticated, configured principal to an existing binding;
request fields never create one. A physical writer owns its transport, intrinsic
checks and durable write intents; a system owns policy and approval/adoption.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
import secrets
import threading

from experiment_core.configuration import Configuration, Parameter
from experiment_core.errors import AuthorizationRequired, Conflict, StaleContext, UncertainWrite
from experiment_core.records import canonical_json, digest, safe_id


@dataclass(frozen=True)
class Binding:
    _guard: object
    _authority: object
    _token: object
    resources: frozenset

    def __reduce__(self):
        raise TypeError("An owner binding cannot be serialized or supplied by RPC")


@dataclass(frozen=True)
class Projection:
    _binding: object
    document: str

    def snapshot(self):
        return json.loads(self.document)

    @property
    def revision(self):
        return digest(self.snapshot())


@dataclass(frozen=True)
class Permit:
    _binding: object
    projection_revision: str
    request_id: str
    operation_digest: str
    resources: frozenset
    cleanup: bool = False

    def __reduce__(self):
        raise TypeError("A write permit cannot be serialized or supplied by RPC")


class SystemPolicyAuthority:
    """Only holder of editable laser policy; callers receive independent copies.

    classify_change is trusted registration code and returns 'quality' only for
    changes with no safety impact. Existing domain approval validators belong in
    authorize_human, never in a physical writer. prepare is read-only; apply may
    install projections; verify must establish settled participant readback.
    """
    def __init__(self, system_id, policy, *, dependencies, journal, scope=None,
                 revision_function=digest, classify_change=None):
        self.system_id = safe_id(system_id)
        self.generation = secrets.token_hex(16)
        self.journal = journal
        self._revision = revision_function
        self._classify = classify_change or (lambda before, after: "safety")
        self._mutex = threading.RLock()
        self._fence_local = threading.local()
        self._candidate = None
        self._bindings = []
        self.configuration = Configuration(journal, scope or system_id + ".policy",
            {"document": Parameter("safety", "", "", "cycle")},
            {"document": deepcopy(policy)}, dependencies=deepcopy(dependencies))
        with journal.guard("system-authority", self.system_id), journal.transaction() as db:
            state = self.configuration.state()
            if state['value']['document'] != policy or state['dependencies'] != dependencies:
                raise StaleContext("Saved authority policy or dependencies differ; reconcile before startup")
            db.execute("CREATE TABLE IF NOT EXISTS projection_owner_generations (role TEXT, identity TEXT, generation TEXT, PRIMARY KEY(role,identity))")
            db.execute("INSERT OR REPLACE INTO projection_owner_generations VALUES ('system',?,?)", (self.system_id, self.generation))
        if self.snapshot() != policy:
            raise StaleContext("Saved authority policy differs; reconcile before startup")

    @contextmanager
    def _fence(self):
        # An adoption may invoke its physical writer on this same thread.
        # Other threads/connections still take the non-reentrant process fence.
        if getattr(self._fence_local, 'held', False):
            self._current()
            yield
            return
        with self.journal.guard("system-authority", self.system_id):
            self._fence_local.held = True
            try:
                self._current()
                yield
            finally:
                self._fence_local.held = False

    def _current(self):
        with self.journal._mutex:
            row = self.journal._db.execute("SELECT generation FROM projection_owner_generations WHERE role='system' AND identity=?", (self.system_id,)).fetchone()
        if row is None or row["generation"] != self.generation:
            raise StaleContext("System owner generation was replaced")

    def snapshot(self):
        self._current()
        return deepcopy(self.configuration.state()["value"]["document"])

    @property
    def revision(self):
        return self._revision(self.snapshot())

    @property
    def _projection_revision(self):
        if self._candidate is None and self.configuration.state()['pending'] is not None:
            raise UncertainWrite('Pending policy adoption requires inspection; new writes are refused')
        return self._revision(self._candidate) if self._candidate is not None else self.revision

    @property
    def dependencies(self):
        self._current()
        return deepcopy(self.configuration.state()["dependencies"])

    def _proposal(self, candidate, expected_revision):
        if self.revision != expected_revision:
            raise StaleContext("System policy changed")
        classification = self._classify(self.snapshot(), deepcopy(candidate))
        parameter = classification if isinstance(classification, Parameter) else Parameter(classification, "", "", "cycle")
        self.configuration.parameters["document"] = parameter
        state = self.configuration.state()
        return self.configuration.propose({"document": deepcopy(candidate)},
            base_revision=state["effective"], dependencies=state["dependencies"])

    def approve(self, candidate, *, expected_revision, authorize_human):
        with self._mutex:
            proposal = self._proposal(deepcopy(candidate), expected_revision)
            return self.configuration.approve(proposal, authorize_human)

    def adopt(self, candidate, *, expected_revision, approval=None, prepare, verify,
              apply=None, boundary="cycle", quality_authorized=False, operational_authorized=False):
        candidate = deepcopy(candidate)
        with self._mutex, self._fence():
            proposal = self._proposal(candidate, expected_revision)
            authority = self

            class Adapter:
                def prepare(self, common_proposal):
                    return prepare(deepcopy(candidate))

                def adopt(self, common_proposal, proof):
                    authority._candidate = deepcopy(candidate)
                    try:
                        if apply is not None:
                            apply(deepcopy(candidate), deepcopy(proof))
                    finally:
                        authority._candidate = None

                def verify(self, common_proposal):
                    actual = deepcopy(verify(deepcopy(candidate)))
                    if (not isinstance(actual, dict) or actual.get("settled") is not True or
                            actual.get("source_revision") != authority._revision(candidate)):
                        raise UncertainWrite("System/projection adoption is not settled")
                    # These are common transaction revisions; concrete policy and
                    # participant projection hashes remain separate in actual.
                    participants = {}
                    for binding in authority._bindings:
                        state = binding._guard.inspect(binding)
                        projection = state.get("projection")
                        if projection is not None:
                            if state['fault'] or projection['policy_revision'] != authority._revision(candidate):
                                raise UncertainWrite("A physical participant has not adopted the candidate policy")
                            participants[binding._guard.instrument_id + ':' + digest(sorted(binding.resources))[:12]] = state['projection_revision']
                    actual['verified_participant_projections'] = participants
                    return dict(settled=True, effective_revision=common_proposal["revision"],
                                projection_revision=common_proposal["revision"], actual=actual)

            return self.configuration.adopt(proposal, Adapter(), boundary=boundary,
                approval_token=approval, quality_authorized=quality_authorized, operational_authorized=operational_authorized)

    def reconcile(self, verify):
        """Resolve a pending adoption from settled readback; never reapply it."""
        with self._mutex, self._fence():
            self._current()
            state = self.configuration.state()
            if state['pending'] is None:
                return state
            proposal = state['pending']['proposal']
            candidate = proposal['changes']['document']
            authority = self
            class Inspector:
                def verify(self, common_proposal):
                    evidence = deepcopy(verify(deepcopy(candidate)))
                    if not isinstance(evidence, dict) or evidence.get('settled') is not True:
                        raise UncertainWrite('Policy adoption is not settled')
                    source = evidence.get('source_revision')
                    if source == authority._revision(candidate):
                        revision = common_proposal['revision']
                    elif source == authority.revision:
                        revision = common_proposal['base_revision']
                    else:
                        raise UncertainWrite('Policy participants report mixed or unknown revisions')
                    for binding in authority._bindings:
                        physical = binding._guard.inspect(binding)
                        projection = physical.get('projection')
                        if physical['fault'] or (projection is not None and projection['policy_revision'] != source):
                            raise UncertainWrite('A physical participant differs from the reconciled policy')
                    return dict(settled=True, effective_revision=revision, projection_revision=revision, actual=evidence)
            return self.configuration.reconcile(Inspector())

    def project(self, binding, constraints, *, dependencies=None):
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired("Binding belongs to another system")
        policy = self._candidate if self._candidate is not None else self.snapshot()
        document = dict(schema="ldcon.policy-projection/v1", system_id=self.system_id,
            system_generation=self.generation, policy_revision=self._projection_revision,
            instrument_id=binding._guard.instrument_id, instrument_generation=binding._guard.generation,
            physical_definition_revision=binding._guard.definition_revision,
            resources=sorted(binding.resources), dependencies=deepcopy(self.dependencies if dependencies is None else dependencies),
            constraints=deepcopy(constraints))
        return Projection(binding, canonical_json(document))

    def permit(self, binding, operation, *, request_id, resources=None):
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired("Binding belongs to another system")
        active = binding._guard.inspect(binding)
        if not active.get("projection_revision"):
            raise AuthorizationRequired("No effective physical projection")
        requested = binding.resources if resources is None else frozenset(resources)
        if not requested or not requested <= binding.resources:
            raise AuthorizationRequired("Operation expands its physical resource scope")
        return Permit(binding, active["projection_revision"], safe_id(request_id), digest(operation), requested)

    def permit_cleanup(self, binding, operation, *, request_id):
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired("Cleanup binding belongs to another system")
        state = binding._guard.inspect(binding)
        projection = state.get("projection") or {}
        recipes = projection.get("constraints", {}).get("cleanup_operations", [])
        if not any(digest(operation) == digest(recipe) for recipe in recipes):
            raise AuthorizationRequired("Cleanup must match a pre-admitted bounded recipe exactly")
        return Permit(binding, state["projection_revision"], safe_id(request_id), digest(operation), binding.resources, True)


class VerifiedSystemSource:
    """Read-only remote source identity; no editable policy or approval backend.

    verify_receipt is trusted transport integration. It must authenticate the
    principal and complete exact envelope (including expiry/order), then return
    a plain verified document. A request's claimed identity is never sufficient.
    """
    def __init__(self, system_id, *, journal, verify_receipt):
        self.system_id = safe_id(system_id)
        if not callable(verify_receipt):
            raise AuthorizationRequired("A trusted transport receipt verifier is required")
        self.journal, self._verify = journal, verify_receipt
        self._bindings = []
        self._state = None
        self._instance = secrets.token_hex(16)
        with journal.guard("verified-source", self.system_id), journal.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projection_owner_generations (role TEXT, identity TEXT, generation TEXT, PRIMARY KEY(role,identity))")
            db.execute("INSERT OR REPLACE INTO projection_owner_generations VALUES ('remote_source',?,?)", (self.system_id, self._instance))

    def _fence(self):
        return self.journal.guard("verified-source", self.system_id)

    def _current(self):
        with self.journal._mutex:
            row = self.journal._db.execute("SELECT generation FROM projection_owner_generations WHERE role='remote_source' AND identity=?", (self.system_id,)).fetchone()
        if row is None or row["generation"] != self._instance:
            raise StaleContext("Remote source binding was replaced")

    @property
    def generation(self):
        self._current()
        if self._state is None:
            raise AuthorizationRequired("No verified remote projection receipt")
        return self._state["system_generation"]

    @property
    def revision(self):
        self._current()
        if self._state is None:
            raise AuthorizationRequired("No verified remote projection receipt")
        return self._state["policy_revision"]

    @property
    def _projection_revision(self):
        return self.revision

    @property
    def dependencies(self):
        self._current()
        if self._state is None:
            raise AuthorizationRequired("No verified remote projection receipt")
        return deepcopy(self._state["dependencies"])

    def receive_projection(self, binding, principal, receipt, *, expected_revision):
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired("Remote projection uses another source binding")
        document = deepcopy(self._verify(principal, receipt, "projection"))
        canonical_json(document)
        if not isinstance(document, dict) or document.get("system_id") != self.system_id:
            raise AuthorizationRequired("Verified projection names another system")
        previous = self._state
        self._state = document
        try:
            result = binding._guard.install(binding, Projection(binding, canonical_json(document)), expected_revision=expected_revision)
        except BaseException:
            self._state = previous
            raise
        return result

    def receive_permit(self, binding, principal, receipt, operation, *, cleanup=False):
        self._current()
        binding._guard._binding(binding)
        if binding._authority is not self:
            raise AuthorizationRequired("Remote operation uses another source binding")
        document = deepcopy(self._verify(principal, receipt, "cleanup" if cleanup else "operation"))
        canonical_json(document)
        state = binding._guard.inspect(binding)
        if (not isinstance(document, dict) or document.get("system_id") != self.system_id or
                document.get("system_generation") != self.generation or
                document.get("instrument_id") != binding._guard.instrument_id or
                document.get("instrument_generation") != binding._guard.generation or
                document.get("policy_revision") != self.revision or
                document.get("projection_revision") != state["projection_revision"] or
                document.get("operation_digest") != digest(operation)):
            raise AuthorizationRequired("Verified physical operation envelope does not match")
        resources = frozenset(document.get("resources", ()))
        if not resources or not resources <= binding.resources:
            raise AuthorizationRequired("Verified operation exceeds its physical scope")
        return Permit(binding, state["projection_revision"], safe_id(document["request_id"]), digest(operation), resources, cleanup)


class PhysicalProjectionGuard:
    """Single-writer check shared by native, ECDL and remote instrument adapters.

    intrinsic_validator(operation, constraints) must validate primitive effects
    from physical readback and immutable instrument calibration/capabilities. It
    must not trust a caller-provided 'safe' or 'approved' flag. writer is the sole
    terminal transport call and executes only after a durable intent.
    """
    def __init__(self, instrument_id, resources, *, definition_revision,
                 intrinsic_validator, journal):
        self.instrument_id = safe_id(instrument_id)
        self.resources = frozenset(resources)
        if not self.resources or not callable(intrinsic_validator):
            raise ValueError("Physical resources and an intrinsic validator are required")
        self.definition_revision = str(definition_revision)
        self.generation = secrets.token_hex(16)
        self.intrinsic_validator = intrinsic_validator
        self.journal = journal
        self._bindings = {}
        with journal.guard("projection", self.instrument_id), journal.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projection_owner_generations (role TEXT, identity TEXT, generation TEXT, PRIMARY KEY(role,identity))")
            db.execute("INSERT OR REPLACE INTO projection_owner_generations VALUES ('physical',?,?)", (self.instrument_id, self.generation))
            db.execute("CREATE TABLE IF NOT EXISTS physical_projections (instrument TEXT PRIMARY KEY, projection TEXT, fault TEXT)")
            db.execute("INSERT OR IGNORE INTO physical_projections VALUES (?,NULL,NULL)", (self.instrument_id,))
            db.execute("CREATE TABLE IF NOT EXISTS physical_projection_scopes (instrument TEXT, scope TEXT, resources TEXT, projection TEXT, PRIMARY KEY(instrument,scope))")

    def _current(self):
        with self.journal._mutex:
            row = self.journal._db.execute("SELECT generation FROM projection_owner_generations WHERE role='physical' AND identity=?", (self.instrument_id,)).fetchone()
        if row is None or row["generation"] != self.generation:
            raise StaleContext("Physical owner generation was replaced")

    def bind(self, authority, resources=None):
        """Trusted startup only; never invoke from request-selected identities."""
        self._current()
        if not isinstance(authority, (SystemPolicyAuthority, VerifiedSystemSource)):
            raise AuthorizationRequired("A trusted local system authority is required")
        authority._current()
        selected = self.resources if resources is None else frozenset(resources)
        if not selected or not selected <= self.resources:
            raise AuthorizationRequired("System binding exceeds instrument resources")
        binding = Binding(self, authority, object(), selected)
        self._bindings[id(binding._token)] = binding
        authority._bindings.append(binding)
        with self.journal.transaction() as db:
            db.execute("INSERT OR IGNORE INTO physical_projection_scopes VALUES (?,?,?,NULL)",
                       (self.instrument_id, digest(sorted(selected)), canonical_json(sorted(selected))))
        return binding

    def _binding(self, binding):
        self._current()
        if (not isinstance(binding, Binding) or binding._guard is not self or
                self._bindings.get(id(binding._token)) is not binding):
            raise AuthorizationRequired("Physical mutation requires a configured host binding")
        binding._authority._current()

    def inspect(self, binding=None):
        self._current()
        if binding is not None:
            self._binding(binding)
        scopes = {digest(sorted(value.resources)) for value in self._bindings.values()}
        scope = digest(sorted(binding.resources)) if binding is not None else next(iter(scopes)) if len(scopes) == 1 else None
        with self.journal._mutex:
            row = self.journal._db.execute("SELECT * FROM physical_projections WHERE instrument=?", (self.instrument_id,)).fetchone()
            rows = self.journal._db.execute("SELECT * FROM physical_projection_scopes WHERE instrument=?", (self.instrument_id,)).fetchall()
        documents = {value["scope"]: json.loads(value["projection"]) for value in rows if value["projection"]}
        projection = documents.get(scope)
        return dict(instrument_id=self.instrument_id, generation=self.generation,
                    definition_revision=self.definition_revision, projection=projection, projections=documents,
                    projection_revision=digest(projection) if projection is not None else None, fault=row["fault"])

    def install(self, binding, projection, *, expected_revision):
        self._binding(binding)
        if not isinstance(projection, Projection) or projection._binding is not binding:
            raise AuthorizationRequired("Projection must originate from the bound system authority")
        candidate = projection.snapshot()
        authority = binding._authority
        if (candidate["instrument_id"] != self.instrument_id or candidate["instrument_generation"] != self.generation or
                candidate["physical_definition_revision"] != self.definition_revision or
                candidate["system_generation"] != authority.generation or candidate["system_id"] != authority.system_id or
                candidate["policy_revision"] != authority._projection_revision or
                frozenset(candidate["resources"]) != binding.resources or candidate["dependencies"] != authority.dependencies):
            raise StaleContext("Projection identity, calibration, dependencies or policy changed")
        with self.journal.guard("projection", self.instrument_id), self.journal.transaction() as db:
            row = db.execute("SELECT * FROM physical_projections WHERE instrument=?", (self.instrument_id,)).fetchone()
            scope = digest(sorted(binding.resources))
            scoped = db.execute("SELECT * FROM physical_projection_scopes WHERE instrument=? AND scope=?", (self.instrument_id, scope)).fetchone()
            current = json.loads(scoped["projection"]) if scoped["projection"] else None
            revision = digest(current) if current is not None else None
            others = db.execute("SELECT * FROM physical_projection_scopes WHERE instrument=? AND scope!=? AND projection IS NOT NULL", (self.instrument_id, scope)).fetchall()
            if any(binding.resources.intersection(json.loads(other["resources"])) for other in others):
                raise Conflict("Another projection owns an overlapping physical resource")
            if row["fault"]:
                raise UncertainWrite("Physical owner is quarantined: " + row["fault"])
            if revision != expected_revision:
                raise Conflict("Physical projection changed")
            db.execute("UPDATE physical_projection_scopes SET projection=? WHERE instrument=? AND scope=?",
                       (projection.document, self.instrument_id, scope))
        return self.inspect(binding)

    def quarantine(self, reason):
        self._current()
        with self.journal.transaction() as db:
            db.execute("UPDATE physical_projections SET fault=? WHERE instrument=?", (str(reason), self.instrument_id))

    def execute_cleanup(self, permit, operation, writer):
        if not isinstance(permit, Permit) or not permit.cleanup:
            raise AuthorizationRequired("A bounded cleanup permit is required")
        return self._execute(permit, operation, writer, cleanup=True)

    def execute(self, permit, operation, writer):
        if isinstance(permit, Permit) and permit.cleanup:
            raise AuthorizationRequired("Cleanup must use the explicit cleanup path")
        return self._execute(permit, operation, writer, cleanup=False)

    def _execute(self, permit, operation, writer, *, cleanup):
        if not isinstance(permit, Permit):
            raise AuthorizationRequired("An exact host-issued physical permit is required")
        self._binding(permit._binding)
        if digest(operation) != permit.operation_digest:
            raise AuthorizationRequired("Operation differs from the exact permitted intent")
        binding, authority = permit._binding, permit._binding._authority
        with authority._fence(), self.journal.guard("projection", self.instrument_id):
            state = self.inspect(binding)
            projection = state["projection"]
            if state["fault"] and not cleanup:
                raise UncertainWrite("Physical owner is quarantined: " + state["fault"])
            if (projection is None or state["projection_revision"] != permit.projection_revision or
                    projection["instrument_generation"] != self.generation or
                    projection["physical_definition_revision"] != self.definition_revision or
                    projection["system_generation"] != authority.generation or
                    (not cleanup and projection["policy_revision"] != authority._projection_revision) or
                    (not cleanup and projection["dependencies"] != authority.dependencies) or
                    not permit.resources <= binding.resources or not permit.resources <= frozenset(projection["resources"])):
                raise StaleContext("Physical grant or effective projection is stale")
            if cleanup and not any(digest(operation) == digest(recipe) for recipe in projection["constraints"].get("cleanup_operations", [])):
                raise AuthorizationRequired("Cleanup differs from the installed bounded recipe")
            constraints = deepcopy(projection["constraints"])
            # Execution-path evidence comes from this guard, never the issuer's
            # document; physical cleanup can impose stricter ordering checks.
            constraints['_execution_cleanup'] = cleanup
            self.intrinsic_validator(deepcopy(operation), constraints)
            request_id = "physical." + digest([self.instrument_id, "cleanup" if cleanup else "operation", permit.request_id])
            request = dict(operation=deepcopy(operation), projection_revision=permit.projection_revision,
                           resources=sorted(permit.resources), system_id=authority.system_id)
            fresh = self.journal.admit(request_id, self.instrument_id, request)
            old = self.journal.get(request_id)
            if not fresh:
                if old["state"] == "succeeded":
                    return old["result"]["value"]
                self.quarantine("An earlier physical intent requires inspection; no replay")
                raise UncertainWrite("Physical request already exists and cannot be replayed")
            sequence = self.journal.transition(request_id, 0, "running", {"kind": "physical_intent"})
            try:
                result = deepcopy(writer())
                canonical_json(result)
                self.journal.transition(request_id, sequence, "succeeded", {"kind": "physical_result"}, {"value": result})
                return result
            except BaseException as error:
                self.quarantine(error)
                self.journal.transition(request_id, sequence, "uncertain", {"kind": "physical_uncertain"},
                                        {"effect": "unknown", "error": str(error)})
                raise

    def reconcile(self, binding, inspect):
        """Readback/quiescence only; never repeats a physical request."""
        self._binding(binding)
        with binding._authority._fence(), self.journal.guard("projection", self.instrument_id):
            state = self.inspect(binding)
            evidence = deepcopy(inspect(state))
            canonical_json(evidence)
            if (not isinstance(evidence, dict) or evidence.get("settled") is not True or
                    evidence.get("quiesced") is not True):
                raise UncertainWrite("Physical writer has not been reconciled and quiesced")
            participants = {resource for projection in state["projections"].values()
                            for resource in projection["resources"]}
            if (participants - binding.resources and
                    set(evidence.get("quiesced_resources", [])) != participants):
                raise UncertainWrite("Shared physical owner requires quiescence evidence for every participant")
            with self.journal.transaction() as db:
                db.execute("UPDATE physical_projections SET projection=NULL,fault=NULL WHERE instrument=?", (self.instrument_id,))
                db.execute("UPDATE physical_projection_scopes SET projection=NULL WHERE instrument=?", (self.instrument_id,))
            return evidence
