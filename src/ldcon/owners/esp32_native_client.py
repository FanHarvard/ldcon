"""Typed RPC clients; never open ESP, CLD USB or SDG instrument connections."""
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable, OwnerUnavailable
from ldcon.core.instrument_client import BrokerError, BrokerTransport, validate_endpoint
from ldcon.owners.esp32_native_owner import digest, validate_policy



class FFTokenLost(DeviceError):
    """The owner refused this FF token: it no longer runs this session (not an uncertain output)."""


# OwnerUnavailable (2026-10-27): the owner process did not answer a READ or liveness RPC (connection refused/reset,
# timeout). Defined in ldcon.core.contracts (shared with the ECDL client); imported above so that
# ``esp32_native_client.OwnerUnavailable`` stays the same class for the native card.
OwnerUnavailable = OwnerUnavailable


def _owner_unavailable(exc, what):
    return OwnerUnavailable('Native owner unavailable (%s): %s: %s' % (what, type(exc).__name__, exc))

class BiasLease:
    _rpc = BrokerTransport._rpc

    def __init__(self, config):
        self.config = config
        self.endpoint = validate_endpoint(config['endpoint'])
        self.timeout_s = 3.
        self.lease = None

    def claim(self, owner):
        result = self._rpc('bias_claim', instrument=self.config['instrument'],
                           resource=self.config['resource'], owner=owner)
        self.lease = result['lease']
        return result

    def status(self):
        return self._rpc('bias_status', instrument=self.config['instrument'], lease=self.lease)

    def release(self, *, native_stopped):
        self._rpc('bias_release', instrument=self.config['instrument'], lease=self.lease,
                  native_stopped=native_stopped)
        self.lease = None


class NativeCurrentPort:
    _rpc = BrokerTransport._rpc

    def __init__(self, device):
        self.config = device
        self.policy = validate_policy(device['control_settings']['native_current'])
        connection = device['connection']
        self.endpoint = validate_endpoint(connection['endpoint'])
        self.device_id = connection['device_id']
        self.timeout_s = 30.
        self.lease = None
        self.uncertain_output = False
        self.active = False
        self.gain_state = None
        self.capabilities = []
        self.ff_profile = device['control_settings'].get('native_ff_sweep')
        if self.ff_profile is not None:
            from ldcon.owners.native_ff_proof import validate_profile
            self.ff_profile = validate_profile(self.ff_profile, self.policy)
        self.ff_profile_digest = digest(self.ff_profile) if self.ff_profile is not None else None
        self.ff_token = None
        self.unwind_timeout_s = (self.ff_profile['unwind_steps']*self.ff_profile['unwind_interval_s']+
            self.ff_profile['missing_timeout_s']+2*self.policy['max_sample_age_s']) if self.ff_profile else 0.

    def _call(self, action, **values):
        return self._rpc(action, device_id=self.device_id, **values)

    def connect(self):
        description = self._call('describe')
        if description['policy_digest'] != digest(self.policy):
            raise DeviceError('Native current policy differs from independent owner')
        if description.get('ff_profile_digest') != self.ff_profile_digest:
            raise DeviceError('Native FF profile differs from independent owner')
        if description.get('fault'):
            raise DeviceError('Native current owner requires review: ' + description['fault'])
        self.uncertain_output = True
        try:
            result = self._call('claim', policy_digest=digest(self.policy))
        except BrokerError as exc:
            if exc.code in ('measurement', 'configuration', 'busy'):
                self.uncertain_output = False
            raise
        self.lease = result['lease']
        self.gain_state = description.get('gain_state')
        self.capabilities = list(description.get('capabilities') or [])   # e.g. 'recenter_ramp' (rule L, v5.9)
        self.policy = validate_policy(description.get('effective_policy', self.policy))
        self.uncertain_output = False
        return self

    def mutate(self, action, **values):
        if self.uncertain_output or self.lease is None:
            raise DeviceError('Native current lease is unavailable or uncertain')
        self.uncertain_output = True
        try:
            result = self._call(action, lease=self.lease, **values)
        except BrokerError as exc:
            if exc.code == 'measurement':
                self.uncertain_output = False
                raise MeasurementUnavailable(str(exc)) from exc
            # 'ownership' (lease/FF token refused) is checked by the owner BEFORE any action, like the codes above:
            # nothing was written (v4.9; R4's refused token left the port 'uncertain' for the rest of the lock).
            if exc.code in ('request', 'range', 'busy', 'configuration', 'action', 'ownership'):
                self.uncertain_output = False
            raise DeviceError(str(exc)) from exc
        except Exception as exc:
            raise DeviceError('Uncertain native RPC; do not replay: ' + str(exc)) from exc
        self.uncertain_output = False
        if action == 'arm':
            self.active = True
        elif action == 'stop':
            self.active = False
        return result

    def read(self):
        try:
            result = self._call('status')
        except OSError as exc:
            raise _owner_unavailable(exc, 'status') from exc
        if result.get('fault'):
            self.uncertain_output = True
            raise DeviceError('Native owner fault: ' + result['fault'])
        if not result.get('fresh'):
            raise MeasurementUnavailable('Native owner cache is stale')
        return result

    def refresh_policy(self, expected=None):
        """Adopt the owner's current effective policy without any write.

        After a dashboard-driven live ``policy_update`` the owner reports a new
        digest; the worker's copy (host move caps, freshness budgets, capture)
        follows it. ``expected`` is the saved laser definition block: the
        refresh is ``pending`` until the owner reports exactly that digest.
        """
        try:
            description = self._call('describe')
        except OSError as exc:
            return dict(status='unavailable', reason='Native owner describe unavailable: ' + str(exc))
        if description.get('fault'):
            return dict(status='unavailable', reason='Native owner fault: ' + str(description['fault']))
        reported = description.get('policy_digest')
        if expected is not None and reported != digest(validate_policy(expected)):
            return dict(status='pending', reason='owner policy digest ' + str(reported)[:12] + ' differs from the saved definition',
                        owner_digest=reported)
        self.gain_state = description.get('gain_state')
        self.policy = validate_policy(description.get('effective_policy', description.get('policy', self.policy)))
        return dict(status='refreshed', owner_digest=reported)

    RECENTRE_WAIT_REASON = 'post-retarget'
    sleep = staticmethod(time.sleep)

    def set_target(self, target_frequency_hz, *, target_ff_study_token=None):
        """ONE target (2026-10-22): the owner ramps its firmware target (and the PI centre with it); bounded.

        Hybrid integral recentre (human decision 5, 2026-10-27): the owner may still be running its own BACKGROUND
        bounded recenter_ramp after the previous step; the next step waits only while that ramp is in flight
        (about one second: steps_planned x update_interval_s, bounded here by the same figure plus a margin)."""
        self._wait_for_post_retarget_recentre()
        return self.mutate('retarget', target_frequency_hz=float(target_frequency_hz),
                           **({'target_ff_study_token':target_ff_study_token} if target_ff_study_token else {}))

    def _wait_for_post_retarget_recentre(self):
        deadline = None
        while True:
            try:
                state = self._call('status')
            except OSError as exc:
                raise _owner_unavailable(exc, 'status') from exc
            recentre = state.get('recentre') or {}
            if not (recentre.get('active') and str(recentre.get('reason', '')).startswith(self.RECENTRE_WAIT_REASON)):
                return state
            if deadline is None:
                interval = float(self.policy.get('update_interval_s', .1))
                remaining = max(0, int(recentre.get('steps_planned') or 0) - int(recentre.get('steps') or 0))
                deadline = time.monotonic() + remaining * interval + 2 * interval + 2.
            if time.monotonic() >= deadline:
                return state              # the owner refuses (busy) with its own reason; nothing is written here
            self.sleep(min(.1, float(self.policy.get('update_interval_s', .1))))

    def target_ff_study_status(self):
        return self._pid_read('target_ff_study_status')

    def target_ff_study_set(self, expected_revision, *, scale, step_hz, duration_s, study_token=None):
        return self._pid_read('target_ff_study_set', expected_revision=expected_revision,
            scale=scale, step_hz=step_hz, duration_s=duration_s, study_token=study_token)

    def target_ff_study_restore(self, expected_revision, study_token):
        return self._pid_read('target_ff_study_restore', expected_revision=expected_revision, study_token=study_token)

    def heartbeat(self):
        # A failed pure liveness RPC is not an output mutation. The owner has
        # its own finite heartbeat deadline and retains its device lease.
        try:
            return self._call('heartbeat', lease=self.lease)
        except OSError as exc:
            raise _owner_unavailable(exc, 'heartbeat') from exc
        except BrokerError as exc:
            if exc.code == 'measurement':
                raise MeasurementUnavailable(str(exc)) from exc
            if exc.code == 'fault':
                # The owner latched (e.g. a refused restart adoption): the output is uncertain, leases retained.
                self.uncertain_output = True
                raise DeviceError('Native owner fault: ' + str(exc)) from exc
            if exc.code == 'ownership':
                # The owner no longer holds THIS lease (cleared state, released by review): nothing was written.
                raise DeviceError('Native lease is no longer held by the owner: ' + str(exc)) from exc
            raise DeviceError(str(exc)) from exc

    def pid_state(self):
        """Read cached gain state without marking an uncertain physical write."""
        return self._pid_read('pid_state')

    def pid_check(self, pid):
        """Cache-only candidate proof. Rejection is a result, not a write fault."""
        return self._pid_read('pid_check', pid=pid)

    def _pid_read(self, action, **values):
        try:
            return self._call(action, lease=self.lease, **values)
        except BrokerError as exc:
            if exc.code == 'measurement':
                raise MeasurementUnavailable(str(exc)) from exc
            # A READ refused for ownership (lease/FF token) wrote nothing; it must not leave the port 'uncertain'
            # (2026-10-22: R4's refused FF heartbeat token made the v4.7 port uncertain until the lock's NORMAL stop,
            # where close() then retained the leases, stopped heartbeating, and the owner faulted "Native client
            # heartbeat expired"). Only an owner fault marks the output uncertain.
            if exc.code == 'fault':
                self.uncertain_output = True
            raise DeviceError(str(exc)) from exc
        except OSError as exc:
            raise _owner_unavailable(exc, action) from exc

    def review_details(self):
        return dict(endpoint=self.endpoint, device_id=self.device_id, lease=self.lease,
                    message='Native/CLD bias leases retained; inspect both independent owners')

    def ff_validate(self, plan):
        from ldcon.owners.native_ff_proof import plan_parameters
        return self._pid_read('ff_validate', plan=plan_parameters(plan), profile_digest=self.ff_profile_digest)

    def ff_begin(self, plan):
        proof = self.ff_validate(plan)
        if not proof.get('ok'):
            raise DeviceError('Native FF plan rejected: '+'; '.join(proof.get('reasons', [])))
        result = self.mutate('ff_begin', plan=proof['plan'], profile_digest=self.ff_profile_digest,
                             proof_digest=proof['proof_digest'])
        self.ff_token = result['token']
        self.unwind_timeout_s = result['unwind_timeout_s']
        return result

    def ff_status(self):
        result = self._pid_read('ff_status', token=self.ff_token, profile_digest=self.ff_profile_digest)
        if self.ff_token is None and result.get('profile_digest') == self.ff_profile_digest:
            self.ff_token = result['token']  # Read-only reconciliation, never replay begin.
        if result.get('state') == 'fault':
            self.uncertain_output = True
        return result

    def ff_heartbeat(self, token=None):
        """FF liveness only. A token refusal ('Exact native feedforward token required': the owner no longer runs
        THIS session -- it ended FF itself, or a new session replaced it) is not an uncertain output (v4.8)."""
        try:
            return self._call('ff_heartbeat', lease=self.lease, token=token or self.ff_token, profile_digest=self.ff_profile_digest)
        except BrokerError as exc:
            if exc.code == 'measurement':
                raise MeasurementUnavailable(str(exc)) from exc
            if exc.code == 'ownership' and 'feedforward token' in str(exc):
                raise FFTokenLost(str(exc)) from exc
            if exc.code == 'fault':
                self.uncertain_output = True
            raise DeviceError(str(exc)) from exc
        except OSError as exc:
            raise _owner_unavailable(exc, 'ff_heartbeat') from exc

    def ff_owner_state(self):
        """Read-only: the owner's current FF record (any token; no reconciliation, no state change here)."""
        try:
            return self._call('ff_status', lease=self.lease, token=None, profile_digest=self.ff_profile_digest)
        except (BrokerError, OSError):
            return None

    def ff_end(self, reason='Requested end'):
        # This RPC records a terminal intent; the owner advances its bounded
        # rollback independently. A lost reply remains uncertain and is never
        # replayed by mutate(). ff_status stays readable for reconciliation.
        return self.mutate('ff_end', token=self.ff_token, profile_digest=self.ff_profile_digest, reason=reason)

    def assembly_start(self, session_request, *, target_hz):
        """Start one exact owner-hosted assembly; reply loss is never auto-replayed."""
        return self.mutate('assembly_start', session_request=session_request, target_hz=target_hz)

    def assembly_status(self, session_id):
        return self._pid_read('assembly_status', session_id=session_id)

    def assembly_trigger(self, session_id, record):
        return self.mutate('assembly_trigger', session_id=session_id, record=record)

    def assembly_finish(self, session_id):
        return self._call('assembly_finish', lease=self.lease, session_id=session_id)

    def assembly_cancel(self, session_id):
        # Durable cancellation is distinct from any ambiguous start or trigger.
        return self._call('assembly_cancel', lease=self.lease, session_id=session_id)

    def close(self):
        if self.lease is not None and not self.uncertain_output:
            if self.ff_token is not None:
                state = self.ff_status()
                if state['state'] not in ('restored', 'fault', 'unwinding'):
                    state = self.ff_end('Native worker stop requested')
                deadline = time.monotonic()+self.unwind_timeout_s
                while state['state'] not in ('restored', 'fault'):
                    if time.monotonic() >= deadline:
                        self.uncertain_output = True
                        raise DeviceError('FF stop deadline exceeded; native and bias leases retained')
                    # Keep the main PI heartbeat alive while its independent
                    # owner safely unwinds. Cancellation never skips cleanup.
                    try:
                        self.heartbeat()
                        state = self.ff_status()
                    except MeasurementUnavailable:
                        pass
                    time.sleep(.05)
                if state['state'] == 'fault':
                    self.uncertain_output = True
                    raise DeviceError('FF stop requires owner review; leases retained')
            # Normal stop is a verified bounded fixed-reference handover.
            self.mutate('stop')
            self.mutate('release')
            self.lease = None
