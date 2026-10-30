import json
import sqlite3
from unittest.mock import patch
import pytest
from ldcon.jobs.recovery_history import Recorder, load, boot_id
from .test_recovery_reference_v2 import config, history


def write_job(root,name,laser='one',created='2026-10-28T00:00:00Z',target=1000.):
    directory=root/name;directory.mkdir()
    (directory/'job.json').write_text(json.dumps(dict(laser={'id':laser},cards=[{'card':'servo.ecdl_steady_lock'}],created_utc=created)))
    with sqlite3.connect(directory/'telemetry.sqlite3') as db:
        db.execute('CREATE TABLE telemetry(id INTEGER PRIMARY KEY, kind TEXT, payload_json TEXT)')
        for index,row in enumerate(history()):
            db.execute('INSERT INTO telemetry VALUES(?,?,?)',(index,'recovery_history_sample',json.dumps(dict(row,laser_id=laser,boot_id=boot_id(),target_frequency_hz=target))))
    return directory


def test_scoped_loader_reads_only_latest_same_laser_current_target(tmp_path):
    root=tmp_path/'jobs';root.mkdir();task=root/'task';task.mkdir()
    write_job(root,'one')
    write_job(root,'foreign',laser='other',created='2026-10-28T00:00:01Z')
    with patch('ldcon.jobs.lock_target.resolve_target',return_value=1000.):
        result=load(task,{'id':'one'},dict(config(),history=[],execution="prepare"),clock=lambda:12.)
    assert len(result['history'])==12 and result['operation_epoch']=='a'
    assert result['event_time_s']==12.
    write_job(root,'new',created='2026-10-28T01:00:00Z',target=2000.)
    with patch('ldcon.jobs.lock_target.resolve_target',return_value=1000.):
        with pytest.raises(ValueError,match='another boot, laser or target'):
            load(task,{'id':'one'},dict(config(),history=[],execution="prepare"),clock=lambda:12.)


def test_recorder_retains_epoch_and_intentional_operation_boundary():
    events=[]; recorder=Recorder('one',lambda kind,row:events.append(row))
    recorder.observe(dict(acquired_s=1.,frequency_hz=1000.,outputs={}),1000.)
    recorder.observe(dict(acquired_s=2.,frequency_hz=1010.,outputs={}),1010.,intentional=True)
    recorder.observe(dict(acquired_s=2.,frequency_hz=1010.,outputs={}),1010.)
    assert len(events)==2
    assert events[0]['operation_epoch']!=events[1]['operation_epoch']
    assert events[1]['intentional_operation']


def test_recorder_freezes_pre_hop_event_and_never_learns_displaced_plateau():
    records=[];recorder=Recorder('one',lambda kind,row:records.append((kind,row)),config())
    for row in history():recorder.observe(row,1000.)
    for stamp in range(12,20):
        recorder.observe(dict(history()[-1],acquired_s=float(stamp),frequency_hz=5000.),1000.)
    events=[r for kind,r in records if kind=='recovery_history_event']
    assert len(events)==1 and events[0]['event_time_s']==12.
    assert events[0]['reference']['frequency_hz']<1002.


def test_loader_does_not_treat_post_hop_plateau_as_a_reference(tmp_path):
    root=tmp_path/'jobs';root.mkdir();task=root/'task';task.mkdir()
    write_job(root,'one')
    with patch('ldcon.jobs.lock_target.resolve_target',return_value=1000.):
        with pytest.raises(ValueError,match='No recorded pre-hop event'):
            load(task,{'id':'one'},dict(config(),history=[]),clock=lambda:12.)


def test_intentional_operation_advances_epoch_without_target_change():
    records=[];recorder=Recorder('one',lambda kind,row:records.append((kind,row)),config())
    for row in history():recorder.observe(row,1000.)
    initial=recorder.epoch
    recorder.observe(dict(history()[-1],acquired_s=12.,frequency_hz=5000.),1000.,intentional=True)
    assert recorder.epoch!=initial and recorder.reference is None
    for stamp in range(13,20):
        recorder.observe(dict(history()[-1],acquired_s=float(stamp),frequency_hz=5000.),1000.)
    assert not [r for kind,r in records if kind=='recovery_history_event']
    assert recorder.reference['frequency_hz']==5000.


def test_resolved_event_is_not_replayed_by_standalone_loader(tmp_path):
    root=tmp_path/'jobs';root.mkdir();task=root/'task';task.mkdir()
    directory=write_job(root,'one')
    with sqlite3.connect(directory/'telemetry.sqlite3') as db:
        event=dict(event_time_s=11.,operation_epoch='a',laser_id='one',boot_id=boot_id(),state='resolved')
        db.execute('INSERT INTO telemetry VALUES(?,?,?)',(100,'recovery_history_event',json.dumps(event)))
    with patch('ldcon.jobs.lock_target.resolve_target',return_value=1000.):
        with pytest.raises(ValueError,match='already resolved'):
            load(task,{'id':'one'},dict(config(),history=[]),clock=lambda:12.)
