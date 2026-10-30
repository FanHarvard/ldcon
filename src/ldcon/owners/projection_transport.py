"""Authenticated system projections over the existing local instrument RPC.

Keys and principal scopes come from host configuration. A caller's identity,
lease, policy document or approval flag alone never authorizes a physical write.
Receipts bind the exact payload, lease, owner generation and monotonic sequence.
"""
import copy
import hashlib
import hmac
import os
import time
import uuid

from experiment_core.errors import AuthorizationRequired, StaleContext
from experiment_core.records import canonical_json, digest, safe_id


def key_from_environment(name):
    if not isinstance(name, str) or not name:
        raise AuthorizationRequired('A projection signing key environment name is required')
    value = os.environ.get(name, '')
    if len(value.encode()) < 32:
        raise AuthorizationRequired('Projection signing key is absent or shorter than 32 bytes: ' + name)
    return value.encode()


def sign(journal, principal, key, kind, document, *, lease, expected_revision=None, clock=time.time):
    safe_id(principal)
    with journal.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS projection_send_sequences (principal TEXT PRIMARY KEY, sequence INTEGER)')
        row = db.execute('SELECT sequence FROM projection_send_sequences WHERE principal=?', (principal,)).fetchone()
        sequence = 1 if row is None else row['sequence'] + 1
        db.execute('INSERT OR REPLACE INTO projection_send_sequences VALUES (?,?)', (principal, sequence))
    issued = clock()
    envelope = dict(schema='ldcon.signed-projection/1', principal=principal, kind=kind,
                    sequence=sequence, issued_at=issued, expires_at=issued + 60.,
                    lease=lease, expected_revision=expected_revision, document=copy.deepcopy(document))
    return dict(envelope=envelope, signature=hmac.new(key, canonical_json(envelope).encode(), hashlib.sha256).hexdigest())


class ReceiptVerifier:
    def __init__(self, journal, principal, key, *, clock=time.time):
        self.journal, self.principal, self.key, self.clock = journal, principal, key, clock
        self.lease = self.expected_revision = None

    def __call__(self, principal, receipt, kind):
        if principal != self.principal or not isinstance(receipt, dict) or set(receipt) != {'envelope', 'signature'}:
            raise AuthorizationRequired('Unknown projection principal or receipt')
        envelope = receipt['envelope']
        actual = hmac.new(self.key, canonical_json(envelope).encode(), hashlib.sha256).hexdigest()
        if not isinstance(receipt['signature'], str) or not hmac.compare_digest(actual, receipt['signature']):
            raise AuthorizationRequired('Projection receipt authentication failed')
        now = self.clock()
        if (envelope.get('schema') != 'ldcon.signed-projection/1' or envelope.get('principal') != principal
                or envelope.get('kind') != kind or envelope.get('lease') != self.lease
                or envelope.get('expected_revision') != self.expected_revision
                or type(envelope.get('sequence')) is not int or envelope['sequence'] <= 0
                or type(envelope.get('issued_at')) not in (int, float)
                or type(envelope.get('expires_at')) not in (int, float)
                or not now - 60. <= envelope['issued_at'] <= now + 5.
                or not now <= envelope['expires_at'] <= envelope['issued_at'] + 60.):
            raise AuthorizationRequired('Projection receipt context, order or expiry is invalid')
        receipt_hash = digest(receipt)
        with self.journal.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS projection_received_sequences (principal TEXT PRIMARY KEY, sequence INTEGER, receipt TEXT)')
            row = db.execute('SELECT * FROM projection_received_sequences WHERE principal=?', (principal,)).fetchone()
            if row is not None and (envelope['sequence'] < row['sequence'] or
                    envelope['sequence'] == row['sequence'] and receipt_hash != row['receipt']):
                raise StaleContext('Projection receipt was superseded')
            db.execute('INSERT OR REPLACE INTO projection_received_sequences VALUES (?,?,?)',
                       (principal, envelope['sequence'], receipt_hash))
        return copy.deepcopy(envelope['document'])


class ProjectionClient:
    """System-owned signing adapter. It contains policy references, never keys in RPC."""
    def __init__(self, authority, link, specification, leases, constraints):
        authority._current()
        self.authority, self.link = authority, link
        self.principal = safe_id(specification.get('principal'))
        self.key = key_from_environment(specification.get('key_env'))
        self.leases = dict(leases)
        self.states = {}
        self.configure(constraints)

    def configure(self, constraints):
        authority = self.authority
        authority._current()
        for channel, lease in self.leases.items():
            state = self.link.rpc('projection_status', lease=lease, principal=self.principal)
            document = dict(schema='ldcon.policy-projection/v1', system_id=authority.system_id,
                system_generation=authority.generation, policy_revision=authority._projection_revision,
                instrument_id=state['instrument_id'], instrument_generation=state['generation'],
                physical_definition_revision=state['definition_revision'],
                resources=['servo' if channel == 0 else 'CH' + str(channel)],
                dependencies=authority.dependencies, constraints=copy.deepcopy(constraints))
            # Cleanup recipes at the transport boundary are exact primitive transactions.
            recipes = []
            for recipe in constraints.get('cleanup_operations', []):
                if 'operations' in recipe:
                    recipes.append(recipe)
                elif recipe.get('method') == 'set':
                    recipes.append(dict(channel=channel, operations=[dict(op='set', data=recipe['args'][0])]))
                elif recipe.get('method') in ('set_dc', 'burst_off') and recipe.get('args', [None])[0] == channel:
                    args = recipe['args']
                    command = ('C%d:BSWV WVTP,DC,OFST,%.8fV' % (channel, args[1])
                               if recipe['method'] == 'set_dc' else 'C%d:BTWV STATE,OFF' % channel)
                    recipes.append(dict(channel=channel, operations=[dict(op='write', data=command),
                                                                    dict(op='query', data='*OPC?')]))
            document['constraints']['cleanup_operations'] = recipes
            receipt = sign(authority.journal, self.principal, self.key, 'projection', document,
                           lease=lease, expected_revision=state['projection_revision'])
            adopted = self.link.rpc('projection_install', lease=lease, principal=self.principal,
                                   expected_revision=state['projection_revision'], receipt=receipt)
            if adopted.get('projection') != document or adopted.get('projection_revision') != digest(document):
                raise StaleContext('Physical owner did not verify the exact projected policy')
            self.states[channel] = adopted

    def transaction(self, channel, operations):
        authority = self.authority
        authority._current()
        if channel not in self.states:
            raise AuthorizationRequired('No system projection covers this physical channel')
        state = self.states[channel]
        projected = state['projection']
        if projected['policy_revision'] != authority._projection_revision:
            raise StaleContext('Remote physical projection does not match effective system policy')
        operation = dict(channel=channel, operations=operations)
        # Only a pre-admitted exact recipe may use the separate cleanup path.
        current = self.link.rpc('projection_status', lease=self.leases[channel], principal=self.principal)
        if (current['generation'] != state['generation'] or current['projection_revision'] != state['projection_revision']):
            raise StaleContext('Remote physical owner or projection changed')
        cleanup = bool(current['fault'])
        if cleanup and operation not in projected['constraints'].get('cleanup_operations', []):
            raise AuthorizationRequired('Physical fault permits only its pre-admitted cleanup recipe')
        document = dict(system_id=authority.system_id, system_generation=authority.generation,
            instrument_id=state['instrument_id'], instrument_generation=state['generation'],
            policy_revision=authority._projection_revision, projection_revision=state['projection_revision'],
            resources=projected['resources'], request_id=uuid.uuid4().hex, operation_digest=digest(operation))
        receipt = sign(authority.journal, self.principal, self.key, 'cleanup' if cleanup else 'operation',
                       document, lease=self.leases[channel])
        return self.link.rpc('transaction', lease=self.leases[channel], operations=operations,
                             principal=self.principal, receipt=receipt, cleanup=cleanup)
