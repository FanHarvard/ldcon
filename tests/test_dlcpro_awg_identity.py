"""The actual dedicated SDG2122X can reach its serial-pinned owner."""
import pytest

from ldcon.drivers.staircase import validate_channels
from ldcon.owners.instrument_identity import identity_from_idn


def channel(instrument):
    return dict(role='dlc_pzt', resource='TCPIP::offline.invalid::INSTR',
                channel=1, profile='siglent_sdg2000x', min_v=-1., max_v=1.,
                max_vpp=2., broker=dict(endpoint='http://127.0.0.1:1',
                                       instrument=instrument))


def test_actual_model_serial_routes_without_alias():
    identity, info = identity_from_idn(
        'Siglent Technologies,SDG2122X,SDG2XFBXA01873,2.01.01.39R7')
    assert identity == 'sdg2122x:SDG2XFBXA01873'
    assert info['firmware'] == '2.01.01.39R7'
    assert validate_channels([channel(identity)])[0]['broker']['instrument'] == identity


@pytest.mark.parametrize('bad', ['sdg2122x:', 'sdg2122x:x/y', 'host:123:extra', None])
def test_invalid_broker_identity_rejected(bad):
    with pytest.raises(ValueError):
        validate_channels([channel(bad)])


def test_legacy_configured_alias_remains_valid():
    assert validate_channels([channel('legacy_awg')])[0]['broker']['instrument'] == 'legacy_awg'


def test_model_does_not_accept_unknown_family():
    with pytest.raises(ValueError, match='Unknown instrument model'):
        identity_from_idn('Siglent Technologies,SDG9999X,SDG2XFBXA01873,2.01')
