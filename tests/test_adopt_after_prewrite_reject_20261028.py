"""Adoption retry after an owner prewrite rejection (2026-10-28, sr422 task 36b6a814).

Hardware sequence: begin -> measure_here x2 -> record_quality_outcome -> adopt
(owner ``prewrite_rejected``: the AWG broker Fizeau frame was unavailable, no
write) -> adopt again at the advanced ``decision_revision``. The retry raised out
of the task as 'unknown' and paused the monitor in ``task_requires_inspection``.
"""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiment_core.records import digest
from ldcon.agents import agent_task, resident_adjustment as runmod
from ldcon.agents.adjustment_quality import QualityReview
from ldcon.core.records import read_json


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


_quality = _load('quality_fixture_20261028', 'test_adjustment_quality.py')
_resident = _load('resident_fixture_20261028', 'test_resident_adjustment.py')
_tasks = _load('tasks_fixture_20261028', 'test_agent_tasks.py')
_monitor = _load('monitor_fixture_20261028', 'test_agent_monitor_history.py')
gateway = _resident.gateway
resident_rig = _resident.resident_rig
task = _tasks.task
monitor = _monitor.monitor

PREWRITE_REASON = ("AWG broker measurement: Fizeau frame unavailable or malformed: "
                   "'NoneType' object has no attribute 'get'")


def prewrite_row(identifier, revision, value=.77, state='prewrite_rejected'):
    return dict(id=identifier, command=dict(action='adopt', based_on=revision, id=identifier, value=value),
                state=state, reason=PREWRITE_REASON, recorded_unix_s=1000.)


def owner_snapshot(row, revision, state='waiting_for_decision'):
    return dict(controller_job_id='controller', latest_result=row,
                status=dict(state=state, segment=0, decision_revision=revision, active=False))


@pytest.fixture
def recorded(tmp_path):
    """An accepted outcome at revision r2, with the owner's real revision chaining."""
    review = QualityReview(tmp_path, _quality.document())
    revision = 'admitted'
    for identifier, start in (('first', 100), ('second', 150)):
        row = _quality.measurement(identifier, start)
        revision = digest([revision, row])
        latest = owner_snapshot(row, revision)
        review.observe(latest)
    review.record(_quality.outcome(), latest)
    return review, revision


def test_prewrite_rejected_adoption_keeps_the_recorded_outcome(recorded):
    review, revision = recorded
    row = prewrite_row('adopt-1', revision)
    revision = digest([revision, row])
    snapshot = owner_snapshot(row, revision)
    review.observe(snapshot)
    review.require_adoption(.77, snapshot)
    assert review.outcome['revision_advanced_by_no_write'] == [
        dict(id='adopt-1', state='prewrite_rejected', action='adopt', reason=PREWRITE_REASON)]
    # Two successive no-write receipts (prewrite rejection, then a stale decision) still chain.
    stale = dict(id='adopt-2', command=dict(action='adopt', based_on='old', id='adopt-2', value=.77),
                 state='stale_decision', reason='Refresh status and replan from current evidence', recorded_unix_s=1001.)
    revision = digest([revision, stale]); snapshot = owner_snapshot(stale, revision)
    review.observe(snapshot)
    review.require_adoption(.77, snapshot)


@pytest.mark.parametrize('change', ['new_measurement', 'different_value', 'rejected_row', 'applied_receipt',
                                    'revoked', 'unobserved_row'])
def test_any_other_change_since_the_outcome_still_refuses_adoption(recorded, change):
    review, revision = recorded
    value = .77
    if change == 'new_measurement':
        row = _quality.measurement('third', 200)
    elif change == 'rejected_row':
        row = prewrite_row('adopt-1', revision, state='rejected')
    elif change == 'applied_receipt':
        row = {**prewrite_row('adopt-1', revision), 'receipt': dict(applied=True, value=.77)}
    else:
        row = prewrite_row('adopt-1', revision)
    if change == 'different_value':
        value = .78
    revision = digest([revision, 'revoked', 'prewrite_rejected']) if change == 'revoked' else digest([revision, row])
    snapshot = owner_snapshot(row, revision)
    if change != 'unobserved_row':
        review.observe(snapshot)
    with pytest.raises(ValueError, match='current quality outcome'):
        review.require_adoption(value, snapshot)
    assert 'revision_advanced_by_no_write' not in review.outcome


def _owner_fake(script):
    """A current-family owner that advances decision_revision exactly like AdaptiveWindowSession._result."""
    owner = dict(revision='admitted', state='not_started', rows=[], adopts=0)
    commands = []

    def api(method, data=None):
        if method == 'POST':
            commands.append(copy.deepcopy(data))
            action = data['action']
            if action == 'begin':
                owner['state'] = 'idle'
                return dict(status='submitted', request_id=data['request_id'])
            if action == 'cancel':
                owner['state'] = 'cancelled'
                owner['rows'].append(dict(id=data['request_id'], state='cancelled', disposition='checkpoint_restored'))
                return dict(status='submitted', request_id=data['request_id'])
            assert data['based_on'] == owner['revision'], 'host submitted stale evidence'
            n = len(commands)
            if action == 'adopt':
                owner['adopts'] += 1
                if script.get('prewrite_adopts', 0) >= owner['adopts']:
                    row = prewrite_row(data['request_id'], data['based_on'], value=data['value'])
                    state = 'waiting_for_decision'
                else:
                    row = _quality.measurement(data['request_id'], 100+50*n, value=data['value'], state='adopted')
                    row['adoption'] = dict(last=dict(state='adopted'))
                    state = 'finished'
            else:
                row = _quality.measurement(data['request_id'], 100+50*n, value=data.get('value', .77))
                state = 'waiting_for_decision'
            owner['rows'].append(row)
            owner['revision'] = digest([owner['revision'], row])
            owner['state'] = state
            return dict(status='submitted', request_id=data['request_id'])
        return dict(operating_mode=dict(mode='measurement', revision=0), grant_active=True,
                    supported_adjustments=['current'], envelope=dict(families=['current']),
                    controller_job_id='controller',
                    status=dict(state=owner['state'], decision_revision=owner['revision'], segment=0, active=False),
                    latest_result=copy.deepcopy(owner['rows'][-1]) if owner['rows'] else None)
    return api, commands


def _record(ids):
    quality = _quality.outcome(); quality.update(measurement_ids=ids, classification='best_practical')
    return dict(action='record_quality_outcome', parameters=quality, reason='Repeated windows at the held point')


BEGIN = dict(action='begin', parameters=dict(family='current'), reason='Dither condition at the held point')
MEASURE = dict(action='measure_here', parameters={}, reason='Fresh owner window')
ADOPT = dict(action='adopt', parameters=dict(value=.77), reason='Adopt the reviewed point')
FINISH = dict(action='finish', parameters={}, reason='Keep the checkpoint; reviewed quality is recorded')


def test_prewrite_rejected_adopt_then_retry_succeeds_against_the_recorded_outcome(resident_rig):
    directory, p, kw, _, _, _ = resident_rig
    api, commands = _owner_fake(dict(prewrite_adopts=1))
    replies = iter([BEGIN, MEASURE, MEASURE, _record(['resident-task-2', 'resident-task-3']), ADOPT, ADOPT])
    result = runmod.run(directory, p, decide=lambda *a: json.dumps(next(replies)), **{**kw, 'api': api})
    assert [c['action'] for c in commands] == ['begin', 'measure_here', 'measure_here', 'adopt', 'adopt']
    assert commands[3]['based_on'] != commands[4]['based_on']
    assert result['status'] == 'succeeded'
    assert result['owner_result']['latest_result']['state'] == 'adopted'
    outcome = result['quality_outcome']
    assert [r['state'] for r in outcome['revision_advanced_by_no_write']] == ['prewrite_rejected']
    assert outcome['post_adoption_meets_old_with_margin'] is True
    history = read_json(directory/'adjustment-decisions.json')
    assert not any('host_validation_rejection' in row for row in history)


def test_new_measurement_after_the_outcome_is_refused_to_the_agent_without_owner_call(resident_rig):
    directory, p, kw, _, _, _ = resident_rig
    api, commands = _owner_fake({})
    replies = iter([BEGIN, MEASURE, MEASURE, _record(['resident-task-2', 'resident-task-3']), MEASURE, ADOPT, FINISH])
    seen = []

    def decide(snapshot, history):
        seen.append(copy.deepcopy(history))
        return json.dumps(next(replies))
    result = runmod.run(directory, p, decide=decide, **{**kw, 'api': api})
    refused = seen[-1][-1]
    assert refused['action'] == 'adopt' and refused['effects'] == 'none' and refused['owner_call'] is False
    assert 'current quality outcome' in refused['host_validation_rejection']
    assert [c['action'] for c in commands] == ['begin', 'measure_here', 'measure_here', 'measure_here', 'cancel']
    assert result['status'] == 'succeeded' and result['disposition'] == 'unchanged'


def test_repeated_host_refusals_end_with_a_known_protocol_stop(resident_rig):
    directory, p, kw, _, _, _ = resident_rig
    api, commands = _owner_fake({})
    replies = iter([BEGIN, MEASURE, ADOPT, ADOPT, ADOPT])
    with pytest.raises(runmod.HostProtocolStop, match='host protocol refusals') as stop:
        runmod.run(directory, p, decide=lambda *a: json.dumps(next(replies)), **{**kw, 'api': api})
    assert stop.value.physical_outcome_known is True
    assert [c['action'] for c in commands] == ['begin', 'measure_here']


@pytest.mark.parametrize('error,status,physical', [
    (runmod.HostProtocolStop('Three consecutive host protocol refusals',
        dict(status=dict(state='waiting_for_decision', active=False), latest_result=dict(state='prewrite_rejected'))),
     'inconclusive', 'no_owner_command_in_flight'),
    (runmod.HostProtocolStop('Three consecutive invalid proposals',
        dict(status=dict(state='waiting_for_decision', active=True), latest_result=dict(state='measured'))),
     'unknown', 'requires_inspection'),
    (runmod.HostProtocolStop('Decision budget reached',
        dict(status=dict(state='waiting_for_decision'), latest_result=dict(state='unknown'))),
     'unknown', 'requires_inspection'),
    (ValueError('Adjustment command rejected; inspect recorded receipt'), 'unknown', 'requires_inspection'),
])
def test_task_status_is_known_only_for_a_protocol_stop_without_uncertain_owner_state(task, monkeypatch, error, status, physical):
    card = dict(task.card, agent_execution=dict(mode='resident'))
    p = dict(task.p, instruction_profile='adjustment')
    monkeypatch.setattr('ldcon.agents.agent_profiles.select_task', lambda *a, **k: (None, None))

    def adjust(*a, **k):
        raise error
    monkeypatch.setattr('ldcon.agents.resident_adjustment.run', adjust)
    (task.directory/'interface-deck.yaml').write_text(json.dumps(dict(schema_version=1, id='deck', name='Deck', cards=[card])))
    from experiment_core.records import atomic_json
    atomic_json(task.directory/'invocation.json', dict(card_id=card['id'], parameters=p))
    run = lambda: agent_task.run(task.directory, p, clock=lambda: 0., sleep=lambda _: None, cancel=lambda: False,
                                 emit=lambda *a: None)
    if status == 'inconclusive':
        outcome = run()
    else:
        with pytest.raises(type(error)):
            run()
        outcome = read_json(task.directory/'agent-task-status.json')
    assert outcome['status'] == status and outcome['physical_outcome'] == physical
    assert read_json(task.directory/'agent-task-status.json')['status'] == status
    if status == 'inconclusive':
        assert outcome['disposition'] == 'host_protocol_stop' and outcome['owner_state'] == 'waiting_for_decision'


@pytest.mark.parametrize('task_status,paused', [('inconclusive', False), ('succeeded', False), ('unknown', True)])
def test_monitor_pauses_only_for_an_unknown_task_outcome(monitor, task_status, paused):
    launched, phases = [], []

    def start(card, prompt, state):
        if card is None:
            job = 'inconclusive' if task_status == 'inconclusive' else 'failed' if task_status == 'unknown' else 'succeeded'
            return dict(status=job, task=dict(status=task_status))
        launched.append(card); return dict(id='task%d' % len(launched))

    def on_sleep(count):
        phases.append(read_json(monitor.directory/'agent-monitor-status.json').get('state'))
    monitor.run(_monitor.params(task_card_id='review', interval_s=1), task_start=start, on_sleep=on_sleep, steps=6)
    assert ('task_requires_inspection' in phases) is paused
    assert (len(launched) > 1) is (not paused)
