"""Exact signed receipts, primitive limits and unknown effects without sockets."""
import copy
import json

import pytest

from experiment_core.records import Journal
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_owner import InstrumentOwner
from ldcon.owners.owner_projection import SystemPolicyAuthority
from ldcon.owners.projection_transport import ProjectionClient


class FakeAWG:
    identity = 'Siglent Technologies,SDG2042X,OFFLINE01,1'
    def __init__(self):
        self.offsets, self.writes, self.fail, self.coupled = {1: 0., 2: 0.}, [], False, False

    def query(self, command):
        if command == '*IDN?':
            return self.identity
        if command == '*OPC?':
            return '1'
        if command == 'COUP?':
            return 'COUP TRACE,%s,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF' % ('ON' if self.coupled else 'OFF')
        channel = int(command[1])
        if command.endswith('BSWV?'):
            return 'C%d:BSWV WVTP,DC,OFST,%gV' % (channel, self.offsets[channel])
        if command.endswith('OUTP?'):
            return 'C%d:OUTP ON,LOAD,HZ,PLRT,NOR' % channel
        return 'C%d:%s STATE,OFF' % (channel, command.split(':')[1][:-1])

    def write(self, command):
        self.writes.append(command)
        if 'OFST,' in command:
            self.offsets[int(command[1])] = float(command.split('OFST,')[1].removesuffix('V'))
        if self.fail:
            raise OSError('Reply lost after physical write')

    def close(self):
        pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('OFFLINE_PROJECTION_KEY', 'synthetic-key-never-used-on-hardware')
    raw = FakeAWG()
    (tmp_path / 'locks').mkdir()
    config = dict(instrument='sdg2042x:OFFLINE01', kind='awg', profile='siglent_sdg2000x',
        host='offline.invalid', expected_identity=raw.identity, channels={'1': {'writes_enabled': True}, '2': {'writes_enabled': True}},
        bind='127.0.0.1', port=1, state_dir=str(tmp_path / 'physical'), lock_dir=str(tmp_path / 'locks'),
        capabilities=dict(voltage_bounds_v=[-2., 2.], max_vpp=4., max_points=32768),
        projection_sources={'laser-a': dict(system_id='laser-a', key_env='OFFLINE_PROJECTION_KEY', channels=[1]),
                            'laser-b': dict(system_id='laser-b', key_env='OFFLINE_PROJECTION_KEY', channels=[2])})
    owner = InstrumentOwner(config, io_factory=lambda *a: raw, resolver=lambda h: '127.0.0.1')
    captured = []
    class Link:
        def rpc(self, action, **values):
            captured.append(copy.deepcopy(dict(action=action, **values)))
            return owner.dispatch(dict(action=action, **values))
    link = Link()
    authorities = []
    clients = []
    for channel, name in ((1, 'laser-a'), (2, 'laser-b')):
        authority = SystemPolicyAuthority(name, {'limit': 1.}, dependencies={}, journal=Journal(tmp_path / (name + '.sqlite3')))
        authorities.append(authority)
        lease = link.rpc('claim', channel=channel, owner=name)['lease']
        constraints = dict(channels={str(channel): dict(bounds=[-1., 1.], max_vpp=2.)}, max_points=1024,
                           cleanup_operations=[])
        clients.append(ProjectionClient(authority, link, {'principal': name, 'key_env': 'OFFLINE_PROJECTION_KEY'}, {channel: lease}, constraints))
    yield owner, raw, link, clients, authorities, captured
    owner.close()
    for authority in authorities:
        authority.journal.close()


def write(value, channel=1):
    return [dict(op='write', data='C%d:BSWV OFST,%gV' % (channel, value))]


def test_lease_or_claimed_identity_does_not_authorize_a_write(rig):
    owner, raw, link, clients, _, _ = rig
    with pytest.raises(BrokerError, match='projection'):
        link.rpc('transaction', lease=clients[0].leases[1], operations=write(.5), principal='laser-a', approved=True)
    assert raw.writes == []
    clients[0].transaction(1, write(.5))
    clients[1].transaction(2, write(-.2, 2))
    assert raw.offsets == {1: .5, 2: -.2}


def test_forged_cross_channel_and_out_of_bounds_operations_are_prewrite_refusals(rig):
    owner, raw, link, clients, _, captured = rig
    client = clients[0]
    client.transaction(1, write(.2))
    original = next(row for row in reversed(captured) if row['action'] == 'transaction')
    tampered = copy.deepcopy(original)
    tampered['operations'] = write(.3)
    with pytest.raises(BrokerError, match='envelope'):
        owner.dispatch(tampered)
    with pytest.raises(BrokerError, match='leased channel'):
        client.transaction(1, write(.2, 2))
    with pytest.raises(BrokerError, match='envelope|projection'):
        client.transaction(1, write(1.01))
    assert raw.writes == ['C1:BSWV OFST,0.2V']
    assert owner.projections.guard.inspect()['fault'] is None


def test_signed_known_result_is_returned_without_repeating_effect_and_old_receipt_is_rejected(rig):
    owner, raw, link, clients, _, captured = rig
    clients[0].transaction(1, write(.2))
    request = copy.deepcopy(captured[-1])
    owner.dispatch(request)
    assert len(raw.writes) == 1
    clients[0].transaction(1, write(.3))
    with pytest.raises(BrokerError, match='superseded'):
        owner.dispatch(request)
    assert len(raw.writes) == 2


def test_unknown_physical_write_retains_lease_and_has_no_replay(rig):
    owner, raw, link, clients, _, captured = rig
    raw.fail = True
    with pytest.raises(BrokerError, match='I/O failed'):
        clients[0].transaction(1, write(.4))
    assert owner.projections.guard.inspect()['fault']
    with pytest.raises(RuntimeError):
        clients[0].transaction(1, write(.4))
    with pytest.raises(BrokerError, match='retain'):
        link.rpc('release', lease=clients[0].leases[1])
    assert len(raw.writes) == 1
    records = owner.projections.journal._db.execute("SELECT state FROM requests").fetchall()
    assert [row['state'] for row in records] == ['uncertain']


def test_coupling_change_refuses_before_write_and_signatures_cover_projection(rig):
    owner, raw, link, clients, _, captured = rig
    installed = copy.deepcopy(next(row for row in captured if row['action'] == 'projection_install'))
    installed['receipt']['envelope']['document']['constraints']['channels']['1']['bounds'] = [-100., 100.]
    with pytest.raises(BrokerError, match='authentication'):
        owner.dispatch(installed)
    raw.coupled = True
    with pytest.raises(BrokerError, match='coupling'):
        clients[0].transaction(1, write(.4))
    assert raw.writes == []


def test_unknown_write_allows_only_exact_predeclared_cleanup_and_keeps_quarantine(rig):
    owner, raw, link, clients, _, _ = rig
    client = clients[0]
    constraints = copy.deepcopy(client.states[1]['projection']['constraints'])
    constraints['cleanup_operations'] = [dict(channel=1, operations=write(0.))]
    client.configure(constraints)
    raw.fail = True
    with pytest.raises(BrokerError):
        client.transaction(1, write(.4))
    raw.fail = False
    client.transaction(1, write(0.))
    assert raw.offsets[1] == 0. and len(raw.writes) == 2
    assert owner.fault and owner.projections.guard.inspect()['fault']
    with pytest.raises(BrokerError, match='retain'):
        link.rpc('release', lease=client.leases[1])
    with pytest.raises(BrokerError, match='cannot admit'):
        client.configure(constraints)
