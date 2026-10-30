"""Observe owner-acknowledged worker epochs without exposing authority tokens."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def observer(tmp_path,monkeypatch):
    path=(Path(__file__).resolve().parent / 'support')/'scripts/test_live_background_handoff.py'
    spec=importlib.util.spec_from_file_location('handoff_observer',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'ADAPTIVE',tmp_path)
    return module


def test_missing_selection_is_not_reported_as_active(observer,tmp_path):
    row=observer.experiment('laser','job')
    assert row['state']=='not_requested' and row['active'] is False


def test_owner_epoch_publication_is_detected_and_tokens_are_redacted(observer,tmp_path):
    root=tmp_path/'laser'/'job';root.mkdir(parents=True)
    (root/'adaptive-experiment.json').write_text(json.dumps(dict(id='task-test')))
    assert observer.experiment('laser','job')['state']=='pending'
    directory=root/'adaptive-experiments'/'task-test'/'adaptive-window'
    directory.mkdir(parents=True)
    status=dict(state='measuring',active=True,target=.7705,epoch=dict(token='private-token',state='active',
        context=dict(lease='private-lease'),checkpoint=dict(current=.77,temperature=.14,target=123)))
    (directory/'status.json').write_text(json.dumps(status))
    seen=observer.experiment('laser','job')
    assert seen['active'] and seen['epoch_state']=='active' and seen['candidate_value']==.7705
    assert seen['checkpoint']==dict(current=.77,temperature=.14)
    assert seen['published_unix_s']<=seen['observed_unix_s']
    assert 'private' not in json.dumps(seen)
    status.update(active=False,state='foreground_ready',returning='foreground')
    (directory/'status.json').write_text(json.dumps(status))
    after=observer.experiment('laser','job')
    assert not after['active'] and after['state']=='foreground_ready'


def test_bad_status_is_not_silently_called_idle(observer,tmp_path):
    root=tmp_path/'laser'/'job';root.mkdir(parents=True)
    (root/'adaptive-experiment.json').write_text('{broken')
    with pytest.raises(ValueError):observer.experiment('laser','job')
