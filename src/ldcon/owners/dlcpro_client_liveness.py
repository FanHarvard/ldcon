"""Credit real controller heartbeats delayed behind a verified owner transaction.

An adjustment never invents controller activity. Only a matching authenticated
heartbeat which actually arrived at Protocol.dispatch can earn one credit while
waiting for the owner mutex. Its normal dispatch still validates the lease.
"""
from contextlib import contextmanager
import threading


class ClientLiveness:
    def __init__(self, protocol, *, request_timeout_s=30.):
        self.protocol = protocol
        self.request_timeout_s = request_timeout_s
        self.lock = threading.Lock()
        self.pending = {}

    @contextmanager
    def incoming(self, request):
        p = self.protocol
        identity = object()
        eligible = (request.get('action') == 'heartbeat'
            and request.get('laser_id') == p.owner.laser_id
            and p.client is not None and request.get('lease') == p.client)
        if eligible:
            with self.lock:
                self.pending[identity] = dict(lease=p.client, arrived_s=p.owner.clock(), credited=False)
        try:
            yield
        finally:
            if eligible:
                with self.lock:
                    self.pending.pop(identity, None)

    def credit_verified_boundary(self):
        """Called only after a parameter transaction's confirmed completion."""
        p = self.protocol
        now = p.owner.clock()
        with self.lock:
            eligible = [r for r in self.pending.values()
                if not r['credited'] and p.client is not None and r['lease'] == p.client
                and 0 <= now-r['arrived_s'] < self.request_timeout_s]
            if not eligible:return False
            # All already accepted requests earn at most this single credit;
            # a stalled queue cannot keep extending the watchdog indefinitely.
            for record in eligible:record['credited'] = True
            p.owner.heartbeat()
            return True
