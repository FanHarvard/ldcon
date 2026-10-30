"""Resume without SessionStart.model must use independent CLI metadata."""
import json
import uuid

import pytest

from ldcon.agents.agent_interactive import InteractiveSessionAdapter
from ldcon.agents import agent_interactive_hooks as hooks

MODEL = 'claude-sonnet-5-5'


@pytest.fixture
def session(tmp_path):
    spool = tmp_path / 'spool'
    sid = str(uuid.uuid4())
    hooks.initialize(spool, sid, tmp_path)
    wire = InteractiveSessionAdapter(dict(provider='claude', model=MODEL,
        effort='medium', request_timeout_s=1), lambda: False)
    wire.spool, wire.session = spool, sid
    wire.alive = lambda: True
    payload = dict(session_id=sid, cwd=str(tmp_path), model=dict(id=MODEL),
                   effort=dict(level='medium'))
    return wire, payload


def test_resumed_session_without_model_uses_statusline_then_dispatches(session):
    wire, payload = session
    hooks.record_event(wire.spool, dict(hook_event_name='SessionStart',
        session_id=wire.session, cwd=payload['cwd'], source='resume'))
    wire.poll()
    assert wire.started and wire.model_info['resolved_model'] is None
    hooks.record_model_status(wire.spool, payload)
    delivered = []
    wire.deliver = delivered.append
    wire.wait_result = lambda: 'confirmed response'
    assert wire.request(dict(prompt='read-only request')) == 'confirmed response'
    assert len(delivered) == 1
    assert wire.model_info['resolved_model'] == MODEL
    assert wire.model_info['resolved_effort'] == 'medium'
    assert wire.model_info['model_confirmation_source'] == 'claude_statusline'


@pytest.mark.parametrize('field,value', [('model', dict(id='claude-opus-5-5')),
                                        ('effort', dict(level='high')),
                                        ('session_id', str(uuid.uuid4())),
                                        ('cwd', '/')])
def test_wrong_model_effort_or_session_never_delivers(session, field, value):
    wire, payload = session
    payload[field] = value
    hooks.record_model_status(wire.spool, payload)
    wire.deliver = lambda _: pytest.fail('No dispatch allowed')
    with pytest.raises(RuntimeError, match='quarantined|protocol'):
        wire.request(dict(prompt='must not be sent'))
    assert wire.quarantined


def test_refreshes_coalesce_but_do_not_hide_model_changes(session):
    wire, payload = session
    for _ in range(100):
        hooks.record_model_status(wire.spool, payload)
    assert len(hooks.read_events(wire.spool)) == 1
    hooks.record_model_status(wire.spool, {**payload, 'model': dict(id='claude-opus-5-5')})
    hooks.record_model_status(wire.spool, payload)
    with pytest.raises(RuntimeError, match='requested profile'):
        wire.poll()
    assert wire.quarantined


def test_status_metadata_is_not_completion_or_secret_storage(session):
    wire, payload = session
    hooks.record_model_status(wire.spool, {**payload, 'last_assistant_message': 'done',
        'transcript_path': '/private/transcript', 'api_key': 'DO_NOT_STORE',
        'workspace': dict(secret='DO_NOT_STORE')})
    wire.poll()
    assert not wire.started and wire.completion is None
    assert wire.model_info['response_confirmed'] is False
    saved = (wire.spool / 'state.json').read_text()
    assert 'DO_NOT_STORE' not in saved and 'transcript' not in saved
    assert json.loads(saved)['last_model_status']['model'] == MODEL


def test_missing_model_is_not_inferred_from_launch_arguments(session):
    wire, _ = session
    wire.deliver = lambda _: pytest.fail('No dispatch without observed model')
    with pytest.raises(RuntimeError, match='unconfirmed'):
        wire.request(dict(prompt='no'))
    assert wire.model_info['resolved_model'] is None
