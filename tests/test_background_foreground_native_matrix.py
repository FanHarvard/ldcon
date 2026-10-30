"""Actual native owner: target and FF admission, idle and mid-adjustment.

All transports are fake; network connections are prohibited by this fixture.
"""
import copy
import importlib.util
from pathlib import Path
import socket
import sys
import time

import pytest

from experiment_core.records import atomic_json
from ldcon.jobs.background_pid_session import background_parameters
from ldcon.owners.flexible_native_trial import pid_from_candidate
from ldcon.core.instrument_client import BrokerError

TESTS = (Path(__file__).resolve().parent / 'support')/'jobs'/'tests'
sys.path.insert(0, str(TESTS))
spec = importlib.util.spec_from_file_location('ff_matrix_fixture', TESTS/'test_native_ff_owner.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
ff_rig = fixture.ff_rig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline matrix must not connect to hardware')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.mark.parametrize('operation', ['target', 'sweep'])
@pytest.mark.parametrize('adjusting', [False, True])
def test_actual_native_foreground_admission_after_confirmed_return(ff_rig,tmp_path,monkeypatch,operation,adjusting):
    owner,servo,clock,bias,optical,call,tick=ff_rig
    original=copy.deepcopy(servo.cfg)
    keys=('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')
    lease=owner.lease
    servo.status['pid_integral']=0.
    if adjusting:
        root=tmp_path/'trusted';(root/'test'/'monitors').mkdir(parents=True)
        controller=root/'test'/'controller';controller.mkdir()
        monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(root))
        p=background_parameters({'parameters':{}},dict(strategy='validated_candidates',save_settings=False))
        grant=dict(enabled=True,generation='one',parameters_hash='rev',last_seen=time.time(),max_age_s=60.,
                   bounds=dict(enabled=True,families=['pi'],pid_parameters=p,handoff_timeout_s=20.))
        atomic_json(root/'test'/'monitors'/'monitor.json',grant)
        atomic_json(controller/'background-control.json',dict(enabled=True,monitor_id='monitor',generation='one'))
        p['candidate']=dict(kp_count_per_nm=original['pid_kp']*1.01,
                            ki_count_per_nm_s=original['pid_ki']*.99,
                            integral_limit_count=abs(original['pid_ki']*original['pid_integral_max'])*1.01)
        started=call('background_begin',family='pi',parameters=p,expected_generation=0,handoff_timeout_s=20.,
                     identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',controller_job_id='controller'))
        token=started['epoch']['token']
        call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']))
        tick(8)
        assert any(servo.cfg[k]!=original[k] for k in keys)
        count=len(servo.ff_writes)
        with pytest.raises(BrokerError,match='returning before'):
            call('retarget',target_frequency_hz=optical.frequency+1e6) if operation=='target' else call('ff_begin',plan=fixture.plan(),proof_digest='not_used_before_return')
        assert len(servo.ff_writes)==count and not owner.retarget
        assert call('background_status')['epoch']['state']=='returning'
        for _ in range(100):
            tick()
            if call('background_status')['foreground_ready']:break
        state=call('background_status')
        assert state['foreground_ready'] and state['last']['state']=='restored'
        assert state['last']['receipt']['confirmed']
        assert all(servo.cfg[k]==original[k] for k in keys)
    else:
        assert call('background_status')['epoch'] is None
    if operation=='target':
        result=call('retarget',target_frequency_hz=optical.frequency+1e6)
        assert result['active']
        tick(10)
        assert not owner.retarget['active'] and owner.retarget['error'] is None
        held_target = servo.cfg['target']
        if adjusting:
            writes = copy.deepcopy(servo.ff_writes)
            with pytest.raises(BrokerError, match='absent or replaced'):
                call('background_restore', token=token, reason='late old task cleanup')
            tick(5)
            assert servo.cfg['target'] == held_target and servo.ff_writes == writes
            newer = call('background_begin', family='pi', parameters=p,
                expected_generation=call('background_status')['generation'], handoff_timeout_s=20.,
                identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',controller_job_id='controller'))
            assert newer['epoch']['checkpoint']['target'] == held_target
            assert newer['epoch']['context']['reference']['target'] == held_target
            call('background_restore', token=newer['epoch']['token'], reason='test complete')
            tick(5)
            assert servo.cfg['target'] == held_target
    else:
        result=fixture.begin(ff_rig)
        assert result['state']=='active'
        call('ff_end',token=result['token'],reason='admission test; no waveform played')
        tick(4)
        assert call('ff_status',token=result['token'])['state']=='restored'
    assert owner.active and owner.lease==lease and owner.fault is None
    assert all(servo.cfg[k]==original[k] for k in keys)
