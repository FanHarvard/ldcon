"""One explicit electrical ON/OFF card, using the laser owner's durable operation."""
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.owners.emission import validate_policy, finite
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_cards import CardCancelled


def configuration(laser):
    from ldcon.jobs.operating_point import lock_device
    _, _, device = lock_device(laser)
    if device['type'] == 'ecdl.rig.owner':
        p = device['control_settings'].get('emission')
    elif device['type'] == 'esp32servocon.native_current.owner':
        p = device['control_settings']['native_current'].get('emission')
    else:
        raise ValueError('No emission owner adapter for this laser')
    p = validate_policy(p or {'enabled': False})
    if not laser['allow_output'] or not p['enabled']:
        raise ValueError('Emission is disabled until its electrical recipe and current monitor are commissioned')
    return device, p


def parameters(card, overrides):
    p = {**card.get('parameters', {}), **overrides}
    if set(p) - {'state','timeout_s'} or p.get('state') not in ('on','off'):
        raise ValueError('Emission parameters are state: on|off and optional timeout_s')
    p.setdefault('timeout_s', 900.)
    if not 1 <= finite(p['timeout_s'], 'timeout_s') <= 1800:
        raise ValueError('Emission card deadline must be 1..1800 seconds')
    return p


class OwnerEmissionPort:
    def __init__(self, laser):
        self.device, self.policy = configuration(laser)
        if self.device['type']=='ecdl.rig.owner':
            from ldcon.jobs.ecdl_devices import OwnerSession
            self.client = OwnerSession(self.device)  # No claim or preparation.
            self.call = self.client.rpc
        else:
            from ldcon.owners.esp32_native_client import NativeCurrentPort
            self.client = NativeCurrentPort(self.device)  # No native/bias claim.
            self.call = self.client._call


def run_emission(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None,
                 owner=None, dashboard=None):
    from ldcon.jobs.dashboard_api import Dashboard
    from ldcon.core.operation_registry import OBSERVERS
    from experiment_core.records import digest
    owner = owner or OwnerEmissionPort(laser)
    dashboard = dashboard or Dashboard.from_job_directory(directory)
    rid = 'emission-' + Path(directory).name
    deadline = clock() + p['timeout_s']
    initial = owner.call('emission_reserve', request_id=rid, state=p['state'], policy_revision=digest(owner.policy))
    emit('emission_transition', initial)
    # OFF intent is already durable. Stop only physical jobs for this laser;
    # independent residents/monitors and every other laser are preserved.
    stopped = set()
    while clock() < deadline:
        if cancel():
            owner.call('emission_cancel', request_id=rid)
            raise CardCancelled('Emission cancelled; inhibit retained, no automatic relight')
        state = owner.call('emission_status')
        if publish:
            publish(state)
        phase = state['phase']
        if state.get('request_id') != rid:
            raise DeviceError('Emission ownership changed; no command replayed')
        if phase in ('output_disabled','current_suppressed','enabled_unlocked'):
            emit('emission_result', state)
            return dict(status='succeeded', **state)
        if phase in ('unknown','cancelled'):
            raise DeviceError('Emission requires inspection: '+str(state.get('reason')))
        if phase == 'quiescing':
            rows = dashboard._request('GET','api/jobs?active_only=true')['jobs']
            physical = []
            for j in rows:
                if j['id'] == Path(directory).name or j.get('laser',{}).get('id') != laser['id']:
                    continue
                operations={c.get('operation',c.get('card')) for c in j.get('cards',[])}
                if operations and operations <= OBSERVERS:
                    continue
                if 'laser.emission' in operations:
                    raise DeviceError('Another emission coordinator is active')
                physical.append(j)
                if j['id'] not in stopped:
                    dashboard._request('POST','api/jobs/'+j['id']+'/stop',{})
                    stopped.add(j['id'])
            if not physical:
                try:
                    owner.call('emission_execute', request_id=rid)
                except BrokerError as error:
                    if error.code != 'busy':
                        raise
                    # Waiting only; no effect is retried. Owner still requires
                    # independent PI/FF/lease cleanup, not terminal job metadata.
        sleep(.2)
    owner.call('emission_cancel', request_id=rid)
    raise DeviceError('Emission deadline reached; inspect owner status, inhibit retained')
