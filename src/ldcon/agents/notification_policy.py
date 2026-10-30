"""Notification-only policy, trusted origin and deterministic short Teams bodies.

Only newly enqueued events are evaluated. Evidence and suppressed/grouped events
remain in the local outbox; changing policy never replays historical deliveries.
"""
import copy
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

PRESETS = ('quiet', 'standard', 'investigation')
EVENTS = frozenset(('healthy', 'failure', 'safety_fault', 'unexpected_stop', 'approval',
                   'recovery', 'material_change', 'adopted_result', 'completion',
                   'operation_start', 'progress', 'startup', 'human_reply'))
IMMEDIATE = EVENTS - {'healthy', 'operation_start', 'progress'}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}')


def defaults():
    return dict(schema_version=1, revision=0,
                global_policy=dict(preset='standard', progress_interval_s=300.),
                lasers={}, tasks={})


def _settings(value, *, partial=False):
    if not isinstance(value, dict) or set(value) - {'preset', 'progress_interval_s'}:
        raise ValueError('Invalid notification settings')
    if not partial and set(value) != {'preset', 'progress_interval_s'}:
        raise ValueError('Notification defaults require preset and progress_interval_s')
    if 'preset' in value and value['preset'] not in PRESETS:
        raise ValueError('Unknown notification preset')
    if 'progress_interval_s' in value:
        interval = value['progress_interval_s']
        if type(interval) not in (float, int) or not math.isfinite(interval) or not 30 <= interval <= 86400:
            raise ValueError('Notification progress interval must be 30..86400 seconds')
    return copy.deepcopy(value)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(defaults()) or value.get('schema_version') != 1:
        raise ValueError('Invalid notification policy schema')
    if type(value['revision']) is not int or value['revision'] < 0:
        raise ValueError('Invalid notification policy revision')
    _settings(value['global_policy'])
    for name in ('lasers', 'tasks'):
        if not isinstance(value[name], dict) or len(value[name]) > 1000:
            raise ValueError('Invalid notification policy scopes')
        for key in value[name]:
            if not isinstance(key, str) or not _ID.fullmatch(key):
                raise ValueError('Invalid notification scope identity')
    for override in value['lasers'].values():
        _settings(override, partial=True)
    for override in value['tasks'].values():
        if (not isinstance(override, dict) or set(override) - {'laser_id', 'policy', 'expires_unix_s', 'completed'}
                or not {'laser_id', 'policy'} <= set(override)
                or not isinstance(override['laser_id'], str) or not _ID.fullmatch(override['laser_id'])):
            raise ValueError('Invalid task notification override')
        _settings(override['policy'], partial=True)
        if 'completed' in override and type(override['completed']) is not bool:
            raise ValueError('Invalid task completion flag')
        if 'expires_unix_s' in override:
            expiry = override['expires_unix_s']
            if type(expiry) not in (float, int) or not math.isfinite(expiry) or not 0 < expiry < 253402300800:
                raise ValueError('Invalid task notification expiry')
    return copy.deepcopy(value)


def location(root):
    configured=os.environ.get('LDCON_NOTIFICATION_POLICY')
    if configured:
        path=Path(configured)
        if not path.is_absolute():raise ValueError('Notification policy path must be absolute')
        return path
    root=Path(root).resolve()
    if root.parent.name in ('deployments','lasers'):root=root.parent.parent/'dashboard'
    return root/'notification-policy.json'


def read_policy(root):
    path = location(root)
    if path.is_symlink():
        raise ValueError('Notification policy must be a regular file')
    if not path.exists():
        return defaults()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Notification policy too large')
    return validate(json.loads(path.read_text()))


def save_policy(root, value, expected_revision):
    from ldcon.core.records import atomic_json
    target=location(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = target.with_suffix('.lock')
    if path.is_symlink():
        raise ValueError('Notification lock must be a regular file')
    value = validate(value)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read_policy(root)
        if type(expected_revision) is not int or old['revision'] != expected_revision:
            raise ValueError('Notification policy changed; refresh before saving')
        value['revision'] = old['revision'] + 1
        atomic_json(target, value)
    return value


def resolve_policy(root, laser_id, task_id='', now=None, *, document=None):
    now = time.time() if now is None else now
    policy = read_policy(root) if document is None else document
    inherited = dict(policy['global_policy'])
    laser = policy['lasers'].get(laser_id, {})
    inherited.update(laser)
    effective = dict(inherited)
    source = 'laser' if laser else 'global'
    override = policy['tasks'].get(task_id)
    expired = bool(override and (override.get('completed') or override.get('expires_unix_s', math.inf) <= now))
    if override and override['laser_id'] == laser_id and not expired:
        effective.update(override['policy'])
        source = 'task'
    return dict(schema_version=1, revision=policy['revision'], effective=effective,
                inherited=inherited, source=source, task_override_expired=expired)


def finish_task(root, task_id):
    """Notification lifetime only. Never changes operational task authority."""
    value = read_policy(root)
    if task_id not in value['tasks'] or value['tasks'][task_id].get('completed'):
        return value
    value['tasks'][task_id]['completed'] = True
    return save_policy(root, value, value['revision'])


def request_context(directory, job_id):
    """Read a host-written active-turn record; never accept origin from a tool."""
    path = Path(directory) / 'agent-current-request.json'
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return dict(origin='automatic')
        value = json.loads(path.read_text())
        if value.get('active') is not True or value.get('job_id') != job_id:
            return dict(origin='automatic')
        return {key: value[key] for key in ('request_id', 'kind', 'origin', 'task_id') if key in value}
    except (OSError, ValueError, AttributeError):
        return dict(origin='automatic')


def parse_notice(encoded):
    if not encoded:
        return {}
    value = json.loads(encoded)
    allowed = {'event', 'event_id', 'case_id', 'operation_id', 'summary', 'action',
               'observed_at', 'lock_state', 'optical_quality', 'facts', 'uncertainty'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Invalid structured notification fields; origin and policy are host-controlled')
    if value.get('event', 'material_change') not in EVENTS - {'human_reply', 'startup'}:
        raise ValueError('Invalid automatic notification event')
    for key in set(value) - {'facts'}:
        if not isinstance(value[key], str) or len(value[key]) > 4000 or '\x00' in value[key]:
            raise ValueError('Invalid structured notification text')
    if 'facts' in value and (not isinstance(value['facts'], list) or len(value['facts']) > 5 or
                            any(not isinstance(item, str) or len(item) > 300 for item in value['facts'])):
        raise ValueError('Invalid notification quantities')
    if value.get('lock_state', 'unverified') not in ('running', 'stopped', 'unverified'):
        raise ValueError('Invalid notification lock state')
    if value.get('optical_quality', 'unverified') not in ('degraded', 'single_mode', 'unverified'):
        raise ValueError('Invalid notification optical quality')
    return value


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z (UTC%z)')
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_sentence(value, fallback, limit=280):
    """Never send arbitrary logs, URLs, credentials or a cut-off numeric scope."""
    if not isinstance(value, str):
        return fallback
    text = ' '.join(value.split())
    unsafe = (not text.isascii() or len(text) > limit or
              re.search(r'https?://|wss?://|(?:^|\s)/[\w./-]+|[{}]|Traceback|\[from\s*:|'
                        r'\b(?:Bearer|Basic)\s|\b(?:password|secret|token|api_key|authorization)\s*[:=]', text, re.I))
    return fallback if not text or unsafe else text


def has_safety_evidence(value):
    if isinstance(value, dict):
        return any((key in {'fault', 'quarantine', 'uncertain_output', 'outputs_uncertain',
                           'lease_unknown', 'lease_retained', 'safety_review'} and bool(item))
                   or has_safety_evidence(item) for key, item in value.items())
    if isinstance(value, list):
        return any(has_safety_evidence(item) for item in value)
    return False


def prepare(meta, card, instance, parameters, payload, *, now, detail='standard'):
    """Prepare a new event. The caller retains parameters/payload as full evidence."""
    notice = dict(payload.get('_notice') or {})
    origin = payload.get('_origin', 'automatic')
    source = payload.get('source', '')
    if instance == 'manual' and source == 'notification_card':
        origin = 'human'
    sender = payload.get('agent_bridge_id', '')
    if sender and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', sender):
        raise ValueError('Invalid notification sender')
    laser = meta.get('laser_id') or meta.get('set_id') or payload.get('laser_id') or 'unidentified laser'
    event = notice.get('event', 'material_change')
    if has_safety_evidence(payload):
        event = 'safety_fault'
    elif source == 'failure_safety_review':
        event = 'safety_fault'
    elif source in ('failure_default', 'failure_report'):
        event = 'failure'
    elif source == 'failure_consult':
        event = 'approval'
    elif payload.get('notice_kind') in ('measurement_timeout', 'measurement_gap', 'capture_loss'):
        event = 'failure'
    elif source == 'agent_startup':
        event = 'startup'
    elif payload.get('status') in ('failed', 'interrupted', 'completed_with_failures'):
        event = 'failure'
    elif payload.get('status') in ('succeeded', 'completed'):
        event = 'completion'
    if origin == 'human' and event not in ('safety_fault', 'approval'):
        event = 'human_reply'
    if event not in EVENTS:
        raise ValueError('Unknown notification event')
    case = payload.get('case_id') or notice.get('case_id')
    if not case and source in ('failure_default', 'failure_safety_review'):
        case = payload.get('event_id')
    operation = notice.get('operation_id') or payload.get('_task_id') or meta['id'] + ':' + card
    group = json.dumps([laser, operation, case or ''], separators=(',', ':'))
    # Approvals correlate the exact question, not a model-chosen notification id.
    if event == 'approval':
        identity = hashlib.sha256(str(payload.get('_exact_approval') or parameters['message']).encode()).hexdigest()
    elif event == 'human_reply':
        identity = payload.get('_request_id') or instance
    else:
        identity = notice.get('event_id') or case or instance
    dedupe = json.dumps([group, event, identity], separators=(',', ':'))
    text = parameters['message']
    exact_human = payload.get('_exact_approval', '') if origin == 'human' and event == 'approval' else ''
    human_approval_safe = bool(exact_human and len(exact_human) <= 4500 and
                               not re.search(r'https?://|\b(?:Bearer|Basic)\s|\b(?:token|password|secret|authorization|api_key)\s*[:=]', exact_human, re.I))
    if human_approval_safe:
        text = ('[from: ' + sender + ']\n' if sender else '') + exact_human
    if human_approval_safe:
        pass  # Preserve the exact human question/reason in its original language.
    elif event in ('human_reply', 'startup') or (origin == 'human' and event != 'approval'):
        # Human wording (and language) and host-generated model/cadence facts survive.
        if len(text) > 6000:
            raise ValueError('Human reply or startup notice exceeds Teams limit; split explicitly')
    else:
        summary = {
            'healthy': 'Routine check found no meaningful change.',
            'failure': 'An operational problem needs attention.',
            'safety_fault': 'Output or ownership uncertainty requires review.',
            'unexpected_stop': 'An unexpected operation stop needs attention.',
            'approval': 'A human decision is required.',
            'recovery': 'Recovery was reported.',
            'material_change': 'A meaningful operational change was reported.',
            'adopted_result': 'An adjustment result was adopted.',
            'completion': 'The operation completed; review its recorded result.',
            'operation_start': 'The operation started.', 'progress': 'An operation milestone was reached.'
        }[event]
        legacy = payload.get('message')
        if not legacy and isinstance(payload.get('data'), dict):
            legacy = payload['data'].get('message')
        if source in ('failure_default', 'failure_report') and payload.get('failure_code'):
            code = _safe_sentence(payload['failure_code'], '', 100)
            if code:
                summary = 'Card failure recorded (' + code + '); recovery is unverified.'
        summary = _safe_sentence(notice.get('summary'), _safe_sentence(legacy, summary))
        if event == 'safety_fault':
            summary = 'Output or ownership uncertainty requires review.'
        observed = _timestamp(notice.get('observed_at'))
        state = notice.get('lock_state', 'unverified') if observed else 'unverified'
        optical = notice.get('optical_quality', 'unverified') if observed else 'unverified'
        state_text = 'Frequency lock: ' + state
        if optical == 'degraded':
            state_text += '; optical quality is degraded'
        elif optical == 'single_mode':
            state_text += '; single-mode evidence reported'
        action = _safe_sentence(notice.get('action'), 'Review the current job and owner; do not replay uncertain actions.')
        if event == 'approval':
            exact = payload.get('_exact_approval', '')
            # An oversized/non-English automatic proposal gets a review alert,
            # never a partly displayed approval scope or an implied yes/no vote.
            safe_exact = bool(exact and len(exact) <= 4500 and (origin == 'human' or exact.isascii())
                              and not re.search(r'https?://|\b(?:Bearer|Basic)\s|\b(?:token|password|secret|authorization|api_key)\s*[:=]', exact, re.I))
            if safe_exact:
                action = 'Decision required (complete question):\n' + exact
            else:
                action = ('Review the complete recorded proposal in a separate approval exchange. '
                          'This alert requests no approval and authorizes no change.')
        if event == 'safety_fault':
            state_text = 'Frequency lock: unverified; outputs or ownership require reconciliation'
            action = 'Review current owner evidence; retain safeguards and do not replay uncertain writes.'
        facts = notice.get('facts', []) if detail != 'quiet' else []
        if facts:
            facts = [_safe_sentence(item, '', 100) for item in facts[:3 if detail == 'investigation' else 2]]
            summary += ''.join(' ' + item for item in facts if item)
        if detail == 'investigation':
            uncertainty = _safe_sentence(notice.get('uncertainty'), '', 160)
            if uncertainty:
                state_text += '; uncertainty: ' + uncertainty
        prefix = '[from: ' + sender + ']' if sender else 'LDCon system notification'
        text = '\n'.join((prefix, str(laser) + ' | ' + summary, state_text + '.',
                          'Action: ' + action, 'Observed: ' + (observed or 'current state unverified; no dated observation supplied') + '.'))
    return dict(schema_version=1, event=event, origin=origin, laser_id=laser,
                task_id=payload.get('_task_id', ''), group_key=group, dedupe_key=dedupe,
                message=text, observed_at=notice.get('observed_at'), request_id=payload.get('_request_id'))


def decide(db, prepared, policy, now):
    """Called inside the outbox write transaction: one event and one decision."""
    db.execute('CREATE TABLE IF NOT EXISTS notification_event_keys(key TEXT PRIMARY KEY, created REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS notification_progress_groups(key TEXT PRIMARY KEY, sent REAL)')
    event = prepared['event']
    effective = policy['effective']
    if event == 'healthy':
        return 'suppressed', 'Unchanged healthy check is local only'
    old = db.execute('SELECT 1 FROM notification_event_keys WHERE key=?', (prepared['dedupe_key'],)).fetchone()
    if old:
        return 'duplicate', 'Same event/case/decision already recorded; no replay'
    db.execute('INSERT INTO notification_event_keys VALUES(?,?)', (prepared['dedupe_key'], now))
    if event in ('operation_start', 'progress'):
        if effective['preset'] != 'investigation':
            return 'grouped', 'Routine progress retained locally; awaiting completion summary'
        previous = db.execute('SELECT sent FROM notification_progress_groups WHERE key=?', (prepared['group_key'],)).fetchone()
        if previous and now - previous[0] < effective['progress_interval_s']:
            return 'grouped', 'Related progress retained locally during notification cooldown'
        db.execute('INSERT OR REPLACE INTO notification_progress_groups VALUES(?,?)', (prepared['group_key'], now))
    return 'pending', 'New event admitted; queue acceptance is not delivery'
