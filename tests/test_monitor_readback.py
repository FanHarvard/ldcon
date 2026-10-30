import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from ldcon.core import operation_registry as operations
from ldcon.agents import agent_startup_notice
from ldcon.core.interface_cards import INTERFACE_CATALOG
from ldcon.core.interface_schema import load_mapping, readiness

SOURCE = dict(endpoint='http://cached-source.invalid', channel=3, cycle=0,
              expected_laser='Fixture', trust_source_clock=True)
LASER = dict(id='fixture', devices={'rig':dict(type='ecdl.rig.owner',connection={'wavemeter':SOURCE})})
DATA = dict(ok=True,channel=3,cycle=0,record=dict(laser='Fixture',aligned=True),
            frequency_thz=710.,t_exposure_end_unix_ms=999000,status=0,quality=0)


@pytest.fixture
def wave():
    from ldcon.web import dashboard_wavelength
    return dashboard_wavelength


def root(tmp_path):
    (tmp_path/'laser-layout.json').write_text(json.dumps(dict(schema='ldcon.single-laser-layout.v1',laser_id='fixture')))
    directory=tmp_path/'config/lasers';directory.mkdir(parents=True)
    (directory/'fixture.yaml').write_text(json.dumps(LASER))
    return tmp_path


def test_wave_without_lock_or_recording_and_without_patterns(tmp_path, wave):
    r=root(tmp_path)
    wave.publish(r,fetcher=lambda s: DATA,clock=lambda:1000.)
    v=wave.view(r,'fixture',LASER,now=1001)
    assert v['valid'] and v['frequency_hz']==710e12 and v['acquired_unix_s']==999
    assert 'endpoint' not in json.dumps(v)
    assert not (r/'jobs').exists()
    assert not wave.view(r,'fixture',LASER,now=1015)['valid']


@pytest.mark.parametrize('patch',[
    dict(channel=4),dict(cycle=1),dict(record=dict(laser='Wrong',aligned=True)),
    dict(record=dict(laser='Fixture',aligned=False)),dict(quality=512),dict(status=1),
    dict(t_exposure_end_unix_ms=900000),dict(t_exposure_end_unix_ms=1001000),
    dict(frequency_thz=float('nan')),dict(record=[]),
])
def test_invalid_wave_is_not_current(patch, wave):
    with pytest.raises(ValueError):wave.measurement(SOURCE,{**DATA,**patch},1000)


def test_failure_and_binding_change_invalidate_success(tmp_path, wave):
    r=root(tmp_path);wave.publish(r,fetcher=lambda s: DATA,clock=lambda:1000.)
    changed=copy.deepcopy(LASER);changed['devices']['rig']['connection']['wavemeter']['channel']=4
    assert not wave.view(r,'fixture',changed,now=1000)['valid']
    def failed(s):raise OSError('private endpoint')
    wave.publish(r,fetcher=failed,clock=lambda:1001.)
    assert not wave.view(r,'fixture',LASER,now=1001)['valid']
    assert 'private endpoint' not in (r/'dashboard-wavelength-fixture.json').read_text()


def test_ambiguous_source_refused(wave):
    laser=copy.deepcopy(LASER)
    laser['devices']['second']=dict(type='fizeau.owner',connection=dict(source={**SOURCE,'channel':4}))
    with pytest.raises(ValueError):wave.binding(laser)


def test_watch_retired_and_history_readable():
    assert 'agent.log_watch' not in INTERFACE_CATALOG
    with pytest.raises(ValueError,match='Retired'):operations.spec('agent.log_watch')
    assert operations.spec('agent.log_watch',historical=True).execution=='retired'
    from ldcon.agents import agent_log_watch
    with pytest.raises(ValueError,match='Retired'):agent_log_watch.run()
    assert operations.spec('agent.monitor').execution=='observer'


def test_startup_schedule_uses_monitor(tmp_path):
    (tmp_path/'interface-deck.yaml').write_text(json.dumps(dict(id='deck',cards=[
        dict(id='monitor',operation='agent.monitor',parameters=dict(agent_card_id='gateway',interval_s=3600)),
        dict(id='old',operation='agent.log_watch',parameters={})])))
    schedules=agent_startup_notice.review_schedules(tmp_path,'gateway')
    assert len(schedules)==1 and schedules[0]['card_id']=='monitor'
    assert schedules[0]['interval_s']==3600 and schedules[0]['execution_confirmed'] is False


def test_configuration_preserves_parameters_and_copies_recovery():
    sys.path.insert(0,str((Path(__file__).resolve().parent / 'support')/'scripts'))
    from monitor_configuration import migrate
    procedure=dict(classes=[dict(id='any',delta_nm=None,sequence=[dict(action='current_excursion',offsets=[-.001])])],
                   home_tolerance_hz=100e6,success_tolerance_hz=50e6)
    deck=dict(id='d',cards=[dict(id='lock',operation='servo.ecdl_steady_lock',parameters={}),
        dict(id='recovery',operation='recover.ecdl_mode_hop',parameters={}),
        dict(id='watch',operation='agent.log_watch',parameters={}),
        dict(id='monitor',operation='agent.monitor',parameters={})])
    assignment=dict(decks=[dict(deck_id='d',parameters=dict(lock=dict(recovery=dict(procedure=procedure)),watch={},monitor=dict(interval_s=1234)))])
    after,a=migrate(deck,assignment,manual_recovery=True)
    assert len(after['cards'])==3
    assert a['decks'][0]['parameters']['recovery']==procedure
    assert a['decks'][0]['parameters']['monitor']==dict(interval_s=1234)
    assert a['decks'][0]['parameters']['lock']==assignment['decks'][0]['parameters']['lock']
    assert 'watch' in assignment['decks'][0]['parameters']
