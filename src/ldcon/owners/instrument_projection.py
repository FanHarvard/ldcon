"""Mandatory projected write boundary for the per-instrument RPC owner."""
import copy

from experiment_core.errors import AuthorizationRequired
from experiment_core.records import Journal, digest

from ldcon.owners.ecdl_projection import operation, physical_validator
from ldcon.owners.owner_projection import PhysicalProjectionGuard, VerifiedSystemSource
from ldcon.owners.projection_transport import ReceiptVerifier, key_from_environment


class InstrumentProjection:
    def __init__(self, owner):
        self.owner = owner
        class Readback:
            def read(self, channel):
                return owner.adapter.readback(channel)
            def query(self, command):
                return owner.adapter.io.query(command)
            def get(self, path):
                return owner.adapter.io.get(path)
        self.validator = physical_validator(Readback(), 'piezo' if owner.config['kind'] == 'esp32servocon' else 'current_awg')
        self.journal = Journal(owner.state_dir / 'physical-operations.sqlite3')
        resources = ['servo' if key == '0' else 'CH' + key for key in owner.config['channels']]
        self.guard = PhysicalProjectionGuard('instrument.' + digest(owner.instrument)[:32], resources,
            definition_revision=owner.definition, intrinsic_validator=self.validate, journal=self.journal)
        self.sources = {}
        for principal, definition in owner.config.get('projection_sources', {}).items():
            verifier = ReceiptVerifier(self.journal, principal, key_from_environment(definition['key_env']), clock=owner.wall_clock)
            source = VerifiedSystemSource(definition['system_id'], journal=self.journal, verify_receipt=verifier)
            bindings = {}
            for channel in definition['channels']:
                resource = 'servo' if channel == 0 else 'CH' + str(channel)
                bindings[channel] = self.guard.bind(source, [resource])
            self.sources[principal] = (source, verifier, bindings)

    def context(self, request, lease):
        try:
            source, verifier, bindings = self.sources[request.get('principal')]
            binding = bindings[lease['channel']]
        except KeyError as exc:
            raise AuthorizationRequired('No configured system binding covers this physical channel') from exc
        verifier.lease = request['lease']
        verifier.expected_revision = request.get('expected_revision')
        return source, binding

    def status(self, request, lease):
        _, binding = self.context(request, lease)
        return self.guard.inspect(binding)

    def install(self, request, lease):
        source, binding = self.context(request, lease)
        return source.receive_projection(binding, request['principal'], request.get('receipt'),
                                         expected_revision=request.get('expected_revision'))

    def execute(self, lease, request, writer):
        source, binding = self.context(request, lease)
        op = dict(channel=lease['channel'], operations=request['operations'])
        cleanup = request.get('cleanup') is True
        permit = source.receive_permit(binding, request['principal'], request.get('receipt'), op, cleanup=cleanup)
        execute = self.guard.execute_cleanup if cleanup else self.guard.execute
        def verified_writer():
            result = writer()
            self.validator.record_result(self.primitive(op))
            return result
        return execute(permit, op, verified_writer)

    @staticmethod
    def primitive(op):
        writes = [entry for entry in op['operations'] if entry['op'] not in ('get', 'query')]
        if len(writes) != 1:
            raise AuthorizationRequired('A projected transaction contains exactly one atomic physical mutation')
        entry = writes[0]
        if entry['op'] == 'write_raw':
            import base64
            return operation('write_raw', [base64.b64decode(entry['data'], validate=True)])
        return operation('set' if entry['op'] == 'set' else '_send', [entry['data']])

    def validate(self, op, constraints):
        owner = self.owner
        # This capability document belongs to the physical instrument definition.
        # It never contains laser safety/calibration policy or request defaults.
        caps = owner.config.get('capabilities')
        if not isinstance(caps, dict):
            raise AuthorizationRequired('Physical capabilities have not been configured')
        projected = copy.deepcopy(constraints)
        if owner.config['kind'] == 'awg':
            bounds, maximum = caps['voltage_bounds_v'], caps['max_vpp']
            for limits in projected.get('channels', {}).values():
                if limits.get('rest_v') is not None and not bounds[0] <= limits['rest_v'] <= bounds[1]:
                    raise AuthorizationRequired('Projected DC rest exceeds physical instrument capabilities')
                limits['bounds'] = [max(bounds[0], limits['bounds'][0]), min(bounds[1], limits['bounds'][1])]
                limits['max_vpp'] = min(maximum, limits.get('max_vpp', maximum))
            projected['max_points'] = min(caps['max_points'], projected.get('max_points', caps['max_points']))
            # Coupling can affect another laser's channel; recheck on every write.
            environment = owner.adapter.environment()
            if environment['reasons'] or environment['identity'] != owner.identity:
                raise AuthorizationRequired('Physical identity/coupling readback does not allow a channel write')
        else:
            bounds = caps['dac_bounds']
            low, high = projected['axis_limits']['bounds']
            projected['axis_limits']['bounds'] = [max(low, bounds[0]), min(high, bounds[1])]

        self.validator(self.primitive(op), projected)
