"""CLD physical primitives: identity, device limits and exact projected grants."""
from contextlib import contextmanager
from copy import deepcopy
import math
import threading
import uuid

from experiment_core.errors import AuthorizationRequired
from experiment_core.records import digest
from ldcon.owners.owner_projection import PhysicalProjectionGuard


class CLDInstrumentOwner:
    def __init__(self, owner, journal):
        self.owner = owner
        self._local = threading.local()
        definition = {key: deepcopy(owner.config[key]) for key in ('device_id', 'resource', 'identity')}
        self.guard = PhysicalProjectionGuard(owner.config['device_id'], {owner.config['resource']},
            definition_revision=digest(definition), intrinsic_validator=self._validate, journal=journal)

    @contextmanager
    def command(self, authority, binding, command):
        operation = {'kind': 'cld_command', 'command': command}
        permit = authority.permit(binding, operation, request_id=uuid.uuid4().hex)
        previous = getattr(self._local, 'grant', None)
        self._local.grant = (permit, operation)
        try:
            yield
        finally:
            self._local.grant = previous

    def write(self, command, writer):
        grant = getattr(self._local, 'grant', None)
        if grant is None or grant[1] != {'kind': 'cld_command', 'command': command}:
            raise AuthorizationRequired('CLD terminal writes require an exact system-owner permit')
        # Consume before dispatch. A nested/second write cannot reuse this grant.
        self._local.grant = None
        return self.guard.execute(grant[0], grant[1], writer)

    def _validate(self, operation, projection):
        if operation.get('kind') != 'cld_command':
            raise ValueError('Only typed CLD primitives are admitted')
        parts = operation.get('command', '').split()
        if len(parts) != 2:
            raise ValueError('CLD primitive must contain one command and one value')
        command, text = parts
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('CLD primitive must be finite')
        controller = self.owner.instrument.io
        sample = controller.read()
        # Rule M step 3: the projection carries the laser's operating bounds; the device rating (layer 1,
        # config.json current_limits_a) and the local activation window still narrow every write here.
        rating = (getattr(self.owner, 'layer1', None) or {}).get('current_limits_a')
        activation = getattr(getattr(getattr(self.owner, 'system_owner', None), 'controller', None), 'current_activation', None)
        def current(candidate):
            bounds = projection.get('current_bounds_a')
            if rating is not None:
                bounds = list(rating) if bounds is None else [max(bounds[0], rating[0]), min(bounds[1], rating[1])]
            if bounds is not None and activation is not None:
                lo, hi = activation['guard'].bounds(activation['activation'])
                bounds = [max(bounds[0], lo), min(bounds[1], hi)]
            if (bounds is None or not bounds[0] <= candidate <= bounds[1] or
                    not 0 <= candidate <= sample['current_limit_a'] or
                    sample['ld_mode'].upper() not in {'CURR', 'CURRENT'}):
                raise ValueError('CLD current violates its physical or projected limits')
        def temperature(candidate):
            scale, offset = controller._unit()
            low = float(controller.query('SOUR2:TEMP:LIM:LOW?')) * scale + offset
            high = float(controller.query('SOUR2:TEMP:LIM:UPP?')) * scale + offset
            bounds = projection.get('temperature_bounds_c')
            if (not math.isfinite(low) or not math.isfinite(high) or bounds is None or
                    not max(low, bounds[0]) <= candidate <= min(high, bounds[1]) or
                    sample['tec_mode'].upper() not in {'TEMP', 'TEMPERATURE'}):
                raise ValueError('CLD temperature violates its physical or projected limits')
        if command == 'SOUR1:CURR':
            if not projection['current_writes_enabled']:
                raise AuthorizationRequired('CLD current writes are not granted')
            current(value)
        elif command == 'SOUR2:TEMP:SPO':
            if not projection['temperature_writes_enabled']:
                raise AuthorizationRequired('CLD temperature writes are not granted')
            scale, offset = controller._unit()
            temperature(value * scale + offset)
        elif command in {'OUTP1', 'OUTP2', 'SOUR1:AM:STAT'}:
            # ld_enable_writes_enabled is a layer-1 permission of this CLD (config.json), not a projected condition.
            permitted=((getattr(self.owner,'layer1',None) or {}).get('ld_enable_writes_enabled',False)
                       if command=='OUTP1' and getattr(self.owner,'emission_interlock',None) is not None else projection['enable_writes_enabled'])
            if value not in (0., 1.) or not permitted:
                raise AuthorizationRequired('CLD output change is not granted')
            if value:
                current(sample['current_a']); temperature(sample['temperature_c'])
            if command == 'OUTP2' and value == 0 and sample['ld_enabled']:
                raise ValueError('CLD LD must be disabled before TEC')
        else:
            raise AuthorizationRequired('CLD modes, limits and raw commands cannot be changed')
