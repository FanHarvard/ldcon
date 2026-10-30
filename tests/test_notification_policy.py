"""Notification contracts through resident tools, durable outbox and dispatcher.

All transports are stubs. No real model, webhook, service or instrument is used.
"""
import json
from types import SimpleNamespace

import pytest

from ldcon.agents import notification_policy as policy
from ldcon.agents.agent_tools import Toolbox
from ldcon.agents.card_fallback import FallbackStore, NotificationDispatcher
from ldcon.agents.teams_card import enqueue, receipt


PARAMS = dict(webhook_env='LDCON_TEAMS_TEST', message='', sample_interval_s=1.)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    job = root / 'jobs' / 'resident1'
    job.mkdir(parents=True)
    meta = dict(id='resident1', laser_id='laser1', set_id='set1', deck_id='deck1')
    (job / 'job.json').write_text(json.dumps(meta))
    context = dict(job_id='resident1', laser_id='laser1', card_id='agent', bridge_id='laser1-agent',
                   provider='codex', workspace=str(job), teams_cards={'teams': PARAMS})
    context_path = job / 'agent-tools-context.json'
    context_path.write_text(json.dumps(context))
    monkeypatch.setenv('LDCON_TEAMS_TEST', 'https://example.webhook.office.com/test')
    sent = []
    monkeypatch.setattr('ldcon.agents.fallback_targets.send_teams', lambda url, body: sent.append(body) or ('sent', 'Teams accepted webhook'))
    queue = FallbackStore(root / 'notifications')
    return SimpleNamespace(root=root, job=job, meta=meta, context=context, context_path=context_path,
                           box=Toolbox(context_path), queue=queue, sent=sent,
                           dispatch=NotificationDispatcher(root, queue))


def facts(event='material_change', **changes):
    return dict(event=event, summary='Optical quality drift detected.',
                action='No action needed from you; adjustment awaits the end of the shot.',
                observed_at='2026-10-24T10:00:00-04:00', lock_state='running',
                optical_quality='degraded', **changes)


def notify(env, request='event1', notice=None, message='Detailed evidence ' * 500):
    args = dict(card_id='teams', request_id=request, message=message)
    if notice is not None:
        args['notice_json'] = json.dumps(notice)
    return env.box.call('teams_notify', args)


def delivery(env, delivery_id):
    with env.queue.connect() as db:
        row = dict(db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone())
    row['payload'] = json.loads(row['payload'])
    return row


def host_request(env, origin, request_id='human1', active=True):
    (env.job / 'agent-current-request.json').write_text(json.dumps(dict(
        origin=origin, active=active, request_id=request_id, job_id=env.meta['id'], kind='request')))


def save(env, preset, interval=300):
    value = policy.read_policy(env.root)
    value['global_policy'] = dict(preset=preset, progress_interval_s=interval)
    return policy.save_policy(env.root, value, value['revision'])


def case(env):
    from ldcon.agents.failure_policy import ensure_case
    meta = dict(env.meta, failure_policy_version=1, failure_targets=dict(lock=dict(
        agent_card_id='agent', teams_card=dict(card_id='teams', parameters=PARAMS))))
    return ensure_case(env.root, meta, 'lock', 'main', dict(status='failed', message='Known failure'))


def test_policy_resolution_cas_expiry_completion_and_laser_isolation(tmp_path):
    value = policy.defaults()
    value['lasers']['laser1'] = dict(preset='quiet')
    value['tasks']['task1'] = dict(laser_id='laser1', policy=dict(preset='investigation', progress_interval_s=90), expires_unix_s=200)
    saved = policy.save_policy(tmp_path, value, 0)
    resolved = policy.resolve_policy(tmp_path, 'laser1', 'task1', now=100)
    assert resolved['source'] == 'task' and resolved['revision'] == 1
    assert resolved['inherited']['preset'] == 'quiet'
    assert resolved['effective'] == dict(preset='investigation', progress_interval_s=90)
    assert policy.resolve_policy(tmp_path, 'laser2', 'task1', now=100)['effective']['preset'] == 'standard'
    assert policy.resolve_policy(tmp_path, 'laser1', 'task1', now=201)['task_override_expired']
    assert policy.resolve_policy(tmp_path, 'laser1', 'task1', now=201)['source'] == 'laser'
    with pytest.raises(ValueError, match='changed'):
        policy.save_policy(tmp_path, saved, 0)
    policy.finish_task(tmp_path, 'task1')
    assert policy.resolve_policy(tmp_path, 'laser1', 'task1', now=100)['effective']['preset'] == 'quiet'


@pytest.mark.parametrize('change', [dict(preset='unlimited'), dict(progress_interval_s=float('nan')),
                                    dict(progress_interval_s=0), dict(hardware_enabled=True)])
def test_policy_rejects_invalid_or_operational_controls(tmp_path, change):
    value = policy.defaults()
    value['global_policy'].update(change)
    with pytest.raises(ValueError):
        policy.save_policy(tmp_path, value, 0)


def test_automatic_summary_complete_send_path_keeps_all_evidence(environment):
    env = environment
    original = '診断全文 ' * 1200 + ' {"token":"not-for-notification"}'
    result = notify(env, notice=facts(), message=original)
    assert result['status'] == 'queued'
    row = delivery(env, result['delivery_id'])
    assert original in row['payload']['message']
    env.dispatch.tick()
    assert len(env.sent) == 1
    body = env.sent[0]
    assert body.startswith('[from: laser1-agent]\n')
    assert body.isascii() and len(body.splitlines()) == 5
    assert 'Frequency lock: running; optical quality is degraded' in body
    assert 'adjustment awaits the end of the shot' in body
    assert '2026-10-24' in body and 'UTC' in body
    assert not any(text in body for text in ('token', '{', 'attachment', 'http', '診断'))
    assert delivery(env, result['delivery_id'])['status'] == 'sent'  # HTTP acceptance, not human confirmation.


def test_missing_dated_evidence_is_unverified_and_old_text_is_not_a_state_proof(environment):
    result = notify(environment, notice=dict(event='failure', lock_state='running'), message='PI=1 locked')
    environment.dispatch.tick()
    assert 'Frequency lock: unverified' in environment.sent[0]
    assert 'current state unverified' in environment.sent[0]
    assert 'PI=1' not in environment.sent[0]
    assert delivery(environment, result['delivery_id'])['payload']['message'].endswith('PI=1 locked')


def test_human_language_correlation_and_no_repeat_with_new_tool_request_id(environment):
    env = environment
    host_request(env, 'human')
    answer = '確認しました。現在のロックは継続しています。'
    first = notify(env, message=answer)
    second = notify(env, request='different-tool-id', message=answer)
    assert first['status'] == 'queued' and second['status'] == 'duplicate'
    env.dispatch.tick(); env.dispatch.tick()
    assert env.sent == ['[from: laser1-agent] ' + answer]
    assert delivery(env, first['delivery_id'])['payload']['notification_summary']['request_id'] == 'human1'
    host_request(env, 'human', active=False)
    notify(env, request='automatic-again', message=answer)
    env.dispatch.tick()
    assert env.sent[-1].isascii()


def test_model_cannot_set_origin_or_policy(environment):
    for notice in (dict(event='healthy', origin='human'), dict(event='progress', preset='investigation')):
        with pytest.raises(ValueError, match='host-controlled'):
            notify(environment, notice=notice)
    assert not environment.sent


def test_healthy_local_and_urgent_events_immediate_in_quiet_mode(environment):
    env = environment
    save(env, 'quiet')
    healthy = notify(env, notice=facts('healthy'))
    failed = notify(env, request='fault', notice=facts('unexpected_stop'))
    assert healthy['status'] == 'suppressed' and failed['status'] == 'queued'
    env.dispatch.tick(); env.dispatch.tick()
    assert len(env.sent) == 1
    suppressed = delivery(env, healthy['delivery_id'])
    assert suppressed['attempts'] == 0 and suppressed['payload']['message']
    assert suppressed['payload']['notification_summary']['policy']['revision'] == 1


def test_progress_grouping_hot_reload_cooldown_and_no_old_backlog(environment, monkeypatch):
    env = environment
    clock = [1000.]
    monkeypatch.setattr('time.time', lambda: clock[0])
    grouped = notify(env, notice=facts('progress', operation_id='search', event_id='point1'))
    assert grouped['status'] == 'grouped'
    save(env, 'investigation', 30)
    first = notify(env, request='p2', notice=facts('progress', operation_id='search', event_id='point2'))
    second = notify(env, request='p3', notice=facts('progress', operation_id='search', event_id='point3'))
    assert first['status'] == 'queued' and second['status'] == 'grouped'
    # Later progress is a new bounded milestone, never a replay of old points.
    with env.queue.connect() as db:
        db.execute('UPDATE notification_progress_groups SET sent=sent-31')
    third = notify(env, request='p4', notice=facts('progress', operation_id='search', event_id='point4'))
    complete = notify(env, request='done', notice=facts('completion', operation_id='search', event_id='done'))
    assert third['status'] == complete['status'] == 'queued'
    for _ in range(5):
        env.dispatch.tick()
    assert len(env.sent) == 3
    assert delivery(env, grouped['delivery_id'])['status'] == 'grouped'
    assert delivery(env, second['delivery_id'])['status'] == 'grouped'


def test_unknown_send_and_same_case_new_id_are_never_replayed(environment, monkeypatch):
    env = environment
    sent = []
    monkeypatch.setattr('ldcon.agents.fallback_targets.send_teams', lambda url, body: sent.append(body) or ('unknown', 'Outcome unknown'))
    first = notify(env, notice=facts('failure', case_id='case1'))
    env.dispatch.tick()
    same = notify(env, notice=facts('failure', case_id='case1'))
    other = notify(env, request='new-request', notice=facts('failure', case_id='case1'))
    save(env, 'investigation')
    env.dispatch.tick()
    assert same == first and other['status'] == 'duplicate' and len(sent) == 1
    assert delivery(env, first['delivery_id'])['status'] == 'unknown'


def test_existing_rows_are_not_reformatted_or_requeued(environment):
    env = environment
    payload = dict(source='notification_card', message='old body', _target='teams_card', _teams_parameters=PARAMS)
    with env.queue.connect() as db:
        db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('old', env.meta['id'], 'teams', 'legacy:teams_card', 1, json.dumps(payload), 'unknown', 1, 0, 'unknown', 1, 1))
    save(env, 'investigation')
    assert enqueue(env.queue, env.meta, 'teams', 'legacy', PARAMS, dict(source='new')) == 'old'
    row = delivery(env, 'old')
    assert row['payload'] == payload and row['status'] == 'unknown'
    env.dispatch.tick()
    assert not env.sent


def test_failure_report_full_path_long_evidence_short_english_and_not_recovery(environment):
    env = environment
    failure = case(env)
    evidence = 'Long diagnostic transcript ' * 500
    result = env.box.call('failure_report', dict(case_id=failure['id'], outcome='repair_reported',
                          summary='調査内容 ' * 500, evidence=evidence, request_id='report1',
                          notice_json=json.dumps(dict(event='recovery', summary='Repair prepared; recovery remains unverified.',
                                                     action='Review the staged repair; reservations remain held.'))))
    assert result['notification'] == 'queued'
    row = delivery(env, result['delivery_id'])
    assert row['payload']['evidence'] == evidence
    assert row['payload']['notification_summary']['event'] == 'failure'
    env.dispatch.tick()
    assert len(env.sent[0]) < 800 and env.sent[0].isascii()
    assert 'recovery remains unverified' in env.sent[0] and evidence not in env.sent[0]


def test_exact_approval_is_complete_and_duplicate_question_not_posted(environment):
    from ldcon.agents.resident_agent import AgentStore
    env = environment
    failure = case(env)
    question = 'Approve only capture_tolerance_hz: 50000000 Hz -> 100000000 Hz for laser1?'
    reason = 'Current observed detuning is 39 MHz; uncertainty is +/- 2 MHz. No other key changes.'
    args = dict(case_id=failure['id'], question=question, reason=reason, request_id='ask1')
    first = env.box.call('failure_consult', args)
    again = env.box.call('failure_consult', dict(args, request_id='ask2'))
    assert first['notification'] == 'queued' and again['notification'] == 'duplicate'
    env.dispatch.tick(); env.dispatch.tick()
    assert len(env.sent) == 1 and question in env.sent[0] and reason in env.sent[0]
    assert AgentStore(env.root).consultation(first['question_id'])['status'] == 'pending'


def test_oversized_approval_is_review_alert_never_partial_scope(environment):
    from ldcon.agents.resident_agent import AgentStore
    env = environment
    failure = case(env)
    exact = 'Approve only these listed changes: ' + ('key: 50 MHz -> 100 MHz; ' * 240) + 'FINAL EXACT SCOPE'
    result = env.box.call('failure_consult', dict(case_id=failure['id'], question=exact,
                                               reason='Exact review required.', request_id='large-approval'))
    env.dispatch.tick()
    assert 'This alert requests no approval and authorizes no change.' in env.sent[0]
    assert '50 MHz' not in env.sent[0] and 'FINAL EXACT SCOPE' not in env.sent[0]
    assert AgentStore(env.root).consultation(result['question_id'])['question'] == exact
    assert delivery(env, result['delivery_id'])['payload']['_exact_approval'].startswith(exact)


def test_owner_uncertainty_cannot_be_downgraded_or_grouped(environment):
    env = environment
    save(env, 'quiet')
    identifier = enqueue(env.queue, env.meta, 'teams', 'fault1', PARAMS,
                         dict(source='failure_default', event_id='case1', fault='quarantine',
                              _notice=dict(event='healthy', summary='Everything is healthy.', lock_state='running',
                                           observed_at='2026-10-24T10:00:00Z')))
    assert receipt(env.queue, identifier)['status'] == 'queued'
    env.dispatch.tick()
    assert 'Output or ownership uncertainty requires review' in env.sent[0]
    assert 'Frequency lock: unverified' in env.sent[0] and 'Everything is healthy' not in env.sent[0]


def test_system_completion_uses_same_body_policy_without_raw_json(environment):
    env = environment
    identifier = enqueue(env.queue, env.meta, 'teams', 'completed1', PARAMS,
                         dict(source='controller', status='succeeded', message='完了' * 100, values=dict(a=1, b=2)))
    env.dispatch.tick()
    assert 'operation completed' in env.sent[0] and env.sent[0].isascii()
    assert '{' not in env.sent[0]
    assert delivery(env, identifier)['payload']['values'] == dict(a=1, b=2)


def test_startup_actual_model_role_cadence_and_reconnect_suppression(environment, monkeypatch):
    from ldcon.agents import agent_startup_notice as startup
    env = environment
    monkeypatch.setattr(startup, 'RECONNECT_PAUSE_FILE', None)
    monkeypatch.setattr(startup, 'review_schedules', lambda directory, card: [dict(interval_s=3600, lookback_s=7200)])
    adapter = SimpleNamespace(model_info=dict(resolved_model='actual-model', response_models=[], response_confirmed=True))
    params = dict(env.context, tools_context=str(env.context_path))
    result = startup.notify_startup(env.job, params, adapter, 'session', reconnected=False, connection_id='a' * 32)
    env.dispatch.tick()
    assert result['status'] == 'notification_queued' and len(env.sent) == 1
    assert all(text in env.sent[0] for text in ('[from: laser1-agent]', 'actual-model', 'Role:', '3600 s', '7200 s', 'state unverified'))
    assert startup.notify_startup(env.job, params, adapter, 'session', reconnected=False, connection_id='a' * 32) == result
    reconnect = startup.notify_startup(env.job, params, adapter, 'session', reconnected=True, connection_id='b' * 32)
    assert reconnect['status'] == 'notification_suppressed'
    env.dispatch.tick()
    assert len(env.sent) == 1


def test_granularity_changes_detail_without_losing_state_or_urgency(environment):
    env = environment
    notice = facts('failure', facts=['Error: 2 MHz.', 'Previous: 1 MHz.'], uncertainty='Sampling coverage is incomplete.')
    save(env, 'quiet')
    quiet = notify(env, request='quiet1', notice=notice)
    env.dispatch.tick()
    save(env, 'standard')
    standard = notify(env, request='standard1', notice=notice)
    env.dispatch.tick()
    save(env, 'investigation')
    investigation = notify(env, request='investigation1', notice=notice)
    env.dispatch.tick()
    assert all(result['status'] == 'queued' for result in (quiet, standard, investigation))
    assert '2 MHz' not in env.sent[0] and '2 MHz' in env.sent[1]
    assert 'Sampling coverage' not in env.sent[1] and 'Sampling coverage' in env.sent[2]
    assert all('Frequency lock: running' in body for body in env.sent)


def test_invalid_saved_preference_does_not_hide_new_safety_notice(environment):
    env = environment
    (env.root / 'notification-policy.json').write_text('{invalid')
    identifier = enqueue(env.queue, env.meta, 'teams', 'urgent', PARAMS,
                         dict(source='failure_safety_review', event_id='fault'))
    env.dispatch.tick()
    assert len(env.sent) == 1
    assert delivery(env, identifier)['payload']['notification_summary']['policy']['source'] == 'invalid_policy_fallback'


def test_human_consultation_keeps_original_question_language(environment):
    env = environment
    host_request(env, 'human')
    failure = case(env)
    question = 'このレーザーの捕捉範囲だけを50 MHzから100 MHzへ変更してよいですか？'
    reason = '誤差は39 MHzです。他の設定は変更しません。'
    result = env.box.call('failure_consult', dict(case_id=failure['id'], question=question,
                                               reason=reason, request_id='jp-approval'))
    env.dispatch.tick()
    assert env.sent == ['[from: laser1-agent]\n' + question + '\nReason: ' + reason]
    assert result['notification'] == 'queued'
