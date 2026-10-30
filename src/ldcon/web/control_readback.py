"""Bounded, read-only operating outputs, independent of a lock worker.

Only the configured controller owner is observed. No client is constructed, no
lease is claimed and no connection or physical output is changed. The frontend
receives values and timestamps, never endpoints, tokens or owner RPC access.
"""
import copy
import threading
import time

from ldcon.agents.agent_owner_status import ECDL, NATIVE, number, observe
from ldcon.core.interface_schema import digest


class ControlReadback:
    def __init__(self, observer=observe):
        self.observer = observer
        self.lock = threading.Lock()
        self.cache = {}

    @staticmethod
    def served(row):
        result = copy.deepcopy(row)
        result['served_unix_s'] = time.time()
        stamp = result.get('sample_unix_s')
        if not number(stamp) or not 0 <= result['served_unix_s'] - stamp <= 5:
            result.update(fresh=False, status='unavailable', outputs={}, references={})
        return result

    def read(self, laser):
        laser_id = laser['id']
        binding = laser.get('interfaces', {}).get('lock') or laser.get('interfaces', {}).get('native_current') or {}
        key = binding.get('device')
        device = laser.get('devices', {}).get(key, {})
        if device.get('type') not in (ECDL, NATIVE):
            return dict(laser_id=laser_id, fresh=False, status='unsupported', read_only=True)
        identity = (laser_id, digest(device))
        with self.lock:
            old = self.cache.get(identity)
            # Coalesce concurrent viewers without adding whole seconds to the
            # operator's 1 Hz readback. Cache age never resets measurement age.
            if old and time.monotonic() - old[0] < .5:
                return self.served(old[1])
            row = self.observer(key, device)
            stamp = row.get('sample_unix_s')
            fresh = (row.get('fresh') is True and not row.get('fault') and
                     number(stamp) and 0 <= time.time() - stamp <= 5)
            result = dict(laser_id=laser_id, device_id=key, owner_type=device['type'],
                          read_only=True, fresh=fresh, sample_unix_s=stamp,
                          read_finished_unix_s=row.get('read_finished_unix_s'),
                          status='observed' if fresh else 'unavailable',
                          pi_stopped=row.get('pi_stopped'), lease_released=row.get('lease_released'),
                          faulted=bool(row.get('fault')), outputs={}, references={})
            if fresh:
                result['outputs'] = {k: v for k, v in row.get('outputs', {}).items()
                                     if k in ('current', 'temperature', 'piezo') and number(v)}
                if device['type'] == NATIVE:
                    if number(row.get('dac')):result['outputs']['native_current'] = row['dac']
                    if number(row.get('fixed_reference')):
                        result['references']['native_current'] = row['fixed_reference']
            if len(self.cache) > 32:self.cache.clear()
            self.cache[identity] = time.monotonic(), result
            return self.served(result)
