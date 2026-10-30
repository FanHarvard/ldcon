"""Route the existing target HTTP contract to an attached DLC owner.

The Sr queued card API is unchanged. DLC requests never claim/release the running
controller lease. After ambiguous transport failure callers use request lookup;
the facade does not resubmit a mutation or infer optical settlement from YAML.
"""
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.dlcpro_cards import Session
from ldcon.owners.dlcpro_target_changes import TargetError, request_body
from ldcon.web.lock_target_api import ApiError, LockTargetApi, REQUEST_ID
from ldcon.web.lock_target_queue import QueuedLockTargetApi


class DLCProTargetApi(LockTargetApi):
    def __init__(self, runtime, *, session_factory=Session, **kwargs):
        super().__init__(runtime, **kwargs)
        self.session_factory = session_factory

    def _rpc(self, laser_id, action, **values):
        laser = self._laser(laser_id)
        device = laser['devices'][laser['interfaces']['dlc_lock']['device']]
        try:
            result = self.session_factory(device).rpc(action, **values)
        except BrokerError as error:
            raise ApiError(409 if error.code in ('configuration', 'ownership') else 503,
                           'owner_refused' if error.code in ('configuration', 'ownership') else 'owner_unavailable',
                           str(error)) from None
        except (OSError, TimeoutError) as error:
            raise ApiError(503, 'owner_unavailable',
                           'Owner response unavailable; look up the same request_id before another target submission') from error
        if 'error' in result:
            error = dict(result['error'])
            raise ApiError(error.pop('status'), error.pop('code'), error.pop('detail'), **error)
        return result

    def get_target(self, laser_id):
        return self._rpc(laser_id, 'target_change_target')

    def start_change(self, laser_id, body):
        try:
            body = request_body(body)
        except TargetError as error:
            payload = dict(error.payload)
            raise ApiError(payload.pop('status'), payload.pop('code'), payload.pop('detail'), **payload) from None
        self.runtime.assert_admission_open()
        result = self._rpc(laser_id, 'target_change_start', body=body)
        return result['status'], result['change']

    def change_state(self, laser_id, change_id):
        return self._rpc(laser_id, 'target_change_read', change_id=change_id)

    def change_by_request(self, laser_id, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ApiError(400, 'invalid_request', 'Invalid request_id')
        return self._rpc(laser_id, 'target_change_read', request_id=request_id)

    def cancel(self, laser_id, change_id):
        result = self._rpc(laser_id, 'target_change_cancel', change_id=change_id)
        return result['status'], result['change']


class TargetApi:
    """Drop-in factory product for route handlers and the queue background tick."""
    def __init__(self, runtime, **kwargs):
        self.runtime = runtime
        self.ordinary = QueuedLockTargetApi(runtime, **kwargs)
        self.dlc = DLCProTargetApi(runtime, **kwargs)

    def _api(self, laser_id):
        laser = self.ordinary._laser(laser_id)
        interface = laser.get('interfaces', {}).get('dlc_lock', {})
        device = laser.get('devices', {}).get(interface.get('device'), {})
        return self.dlc if device.get('type') == 'dlcpro.laser.owner' else self.ordinary

    def get_target(self, laser_id):
        return self._api(laser_id).get_target(laser_id)

    def start_change(self, laser_id, body):
        return self._api(laser_id).start_change(laser_id, body)

    def change_state(self, laser_id, change_id):
        return self._api(laser_id).change_state(laser_id, change_id)

    def change_by_request(self, laser_id, request_id):
        return self._api(laser_id).change_by_request(laser_id, request_id)

    def cancel(self, laser_id, change_id):
        return self._api(laser_id).cancel(laser_id, change_id)

    def tick(self):
        self.ordinary.tick()
