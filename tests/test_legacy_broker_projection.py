"""The compatibility broker cannot turn a raw channel lease into write authority."""
import base64

import pytest

from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_service import Hub


def broker(**kwargs):
    return Hub(dict(awg=dict(resource='TCPIP::offline.invalid::INSTR',
        profile='siglent_sdg2000x', channels=[2])), **kwargs)


def claim(hub):
    return hub.dispatch(dict(action='claim', instrument='awg', channel=2,
        resource='TCPIP::offline.invalid::INSTR', profile='siglent_sdg2000x'))['lease']


@pytest.mark.parametrize('simulate', [False, True])
def test_unprojected_lease_refuses_before_transport_open_even_in_simulation(simulate):
    opened = []
    hub = broker(simulate=simulate, factory=lambda config: opened.append(config))
    lease = claim(hub)
    for operation in (dict(op='write', data='C2:BSWV WVTP,DC,OFST,0'),
                      dict(op='write_raw', data=base64.b64encode(b'C2:WVDT LENGTH,4,WAVEDATA,\x00\x00\x00\x00').decode())):
        with pytest.raises(BrokerError, match='read-only'):
            hub.dispatch(dict(action='transaction', instrument='awg', lease=lease, operations=[operation],
                              principal='claimed-laser', approved=True))
    assert opened == []
    assert hub.instruments['awg'].fault is None
    assert hub.status()['instruments']['awg']['write_authority'] == 'read_only_unprojected'


def test_relay_preserves_signed_projection_context_without_minting_authority():
    hub = broker()
    forwarded = []
    class Relay:
        instrument = 'sdg2042x:OFFLINE'
        link = None
        def forward(self, action, **values):
            forwarded.append(dict(action=action, **values))
            return dict(lease='owner-lease') if action == 'claim' else dict(status='owner-result')
    hub.instruments['awg'].relay = Relay()
    lease = claim(hub)
    receipt = dict(envelope={'document': 'opaque authenticated value'}, signature='unchanged')
    for action in ('projection_status', 'projection_install', 'transaction'):
        values = dict(action=action, instrument='awg', lease=lease, principal='bound-laser',
                      expected_revision='owner-cas', receipt=receipt, cleanup=True)
        if action == 'transaction':
            values['operations'] = [dict(op='write', data='C2:BSWV OFST,0')]
        result = hub.dispatch(values)
        assert result == dict(status='owner-result')
        assert forwarded[-1] == {key:value for key,value in values.items() if key != 'instrument'}


def test_rpc_cannot_bind_a_system_object():
    hub = broker()
    with pytest.raises(BrokerError, match='unsupported'):
        hub.dispatch(dict(action='bind_shared_awg', instrument='awg', authority={'approved':True}))
    with pytest.raises(ValueError, match='routed channels'):
        hub.bind_shared_awg('awg', object(), {}, [2])
