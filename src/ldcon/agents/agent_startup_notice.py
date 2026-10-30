"""One durable Teams startup notice per successful resident connection.

Uses frozen deck configuration and the existing notification queue. No webhook
URL, authentication material, service operation, or hardware command is read.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

RECONNECT_PAUSE_FILE = os.environ.get('LDCON_RECONNECT_PAUSE_FILE')

ROLE = 'Laser request gateway, conversations, and task delegation; independent agent-monitor cards schedule reviews and approved adjustment tasks'


def review_schedules(directory, resident_card):
    from ldcon.agents.agent_monitor import parameters
    from ldcon.core.interface_schema import load_mapping
    directory = Path(directory)
    if not (directory / 'interface-deck.yaml').is_file():
        return []
    deck = load_mapping((directory / 'interface-deck.yaml').read_text())
    assignment = load_mapping((directory / 'set.yaml').read_text()) if (directory / 'set.yaml').is_file() else {}
    overrides = next((d.get('parameters', {}) for d in assignment.get('decks', [])
                      if d.get('deck_id', d.get('id')) == deck.get('id')), {})
    result = []
    for card in deck.get('cards', []):
        if card.get('operation') != 'agent.monitor':
            continue
        p = parameters(card, overrides.get(card['id'], {}))
        if p['agent_card_id'] == resident_card:
            result.append(dict(card_id=card['id'], interval_s=p['interval_s'], lookback_s=p['lookback_s'],
                               execution_confirmed=False, enabled=p['enabled'], condition_triggered=bool(p['conditions'])))
    return result


def launch_model(p, adapter):
    info = dict(provider=p['provider'], model_option=None, effort_option=None,
                resolved_model=None, response_models=[], response_confirmed=False)
    observed = getattr(adapter, 'model_info', {})
    valid = lambda value: isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}', value)
    if valid(observed.get('resolved_model')):
        info['resolved_model'] = observed['resolved_model']
    info['response_models'] = [v for v in observed.get('response_models', []) if valid(v)]
    info['response_confirmed'] = observed.get('response_confirmed') is True
    # The exec wrapper records only safe metadata. Match this exact CLI PID and
    # registered laser id; do not reuse a previous session's launch options.
    path = Path(p['workspace']) / 'agent/launch-audit.jsonl'
    pid = getattr(getattr(adapter, 'proc', None), 'pid', None)
    if pid is not None and path.is_file() and not path.is_symlink():
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            lines = stream.read(65536).decode('utf-8', errors='replace').splitlines()
        for raw in reversed(lines):
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if value.get('pid') != pid or value.get('bridge_id') != p['bridge_id']:
                continue
            if valid(value.get('model_option')):
                info['model_option'] = value['model_option']
            if value.get('effort_option') in ('low', 'medium', 'high', 'max'):
                info['effort_option'] = value['effort_option']
            break
    return info


def startup_message(bridge_id, model, schedules, reconnected):
    from ldcon.agents.agent_tools import teams_message
    actual = ', '.join(model['response_models']) or model['resolved_model']
    selected = model['model_option'] or 'unconfirmed'
    effort = model['effort_option'] or 'unconfirmed'
    model_text = actual + ' (resolved)' if actual else 'resolved model unconfirmed'
    frequency = '; '.join((f"every {s['interval_s']:g} s, lookback {s['lookback_s']:g} s" if s['interval_s'] else "condition-triggered") for s in schedules) or 'on request; no periodic review configured'
    response = 'initial response confirmed' if model['response_confirmed'] else 'initial response unconfirmed'
    observed = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z (UTC%z)')
    body = (('Reconnected' if reconnected else 'Started') + '. Model: ' + model_text +
            f", selected={selected}, effort={effort}; {response}.\nRole: {ROLE}.\n" +
            f'Review frequency: {frequency} (configured); unchanged healthy checks remain local.\n' +
            f'Observed: {observed}; laser state unverified by startup.')
    return teams_message(bridge_id, body)


def notify_startup(directory, p, adapter, session, *, reconnected, connection_id, toolbox_factory=None):
    from ldcon.agents.agent_tools import Toolbox
    from ldcon.core.records import atomic_json, read_json
    directory = Path(directory)
    if not re.fullmatch(r'[a-f0-9]{32}', connection_id):
        raise ValueError('A unique resident connection id is required')
    record_path = directory / 'agent-startup-notifications' / (connection_id + '.json')
    latest = directory / 'agent-startup-notification.json'
    if record_path.exists():
        return read_json(record_path)  # A previous unknown delivery is never replayed.
    context = read_json(Path(p['tools_context']))
    model = launch_model(p, adapter)
    schedules = review_schedules(directory, context['card_id'])
    message = startup_message(p['bridge_id'], model, schedules, reconnected)
    record = dict(schema_version=1, connection_id=connection_id, session_id=session,
                  created_utc=datetime.now(timezone.utc).isoformat(),
                  bridge_id=p['bridge_id'], reconnected=reconnected, role=ROLE, model=model,
                  message_language='en',
                  review_schedules=schedules, message=message, delivered=False,
                  request_id='agent-start:' + connection_id, status='notification_intent')
    candidates = context.get('teams_cards', {})
    candidates = {k: v for k, v in candidates.items() if v.get('webhook_env')}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    if reconnected and (RECONNECT_PAUSE_FILE is None or os.path.lexists(RECONNECT_PAUSE_FILE)):
        record['status'] = 'notification_suppressed'
        record['reason'] = 'Reconnect notifications suppressed by deployment policy or unconfigured pause-marker binding; other notifications remain enabled'
    elif not candidates:
        record['status'] = 'notification_not_configured'
    else:
        card = 'teams-notification' if 'teams-notification' in candidates else sorted(candidates)[0]
        record['teams_card_id'] = card
        atomic_json(record_path, record)  # Before enqueue; crashes remain uncertain.
        try:
            if toolbox_factory:
                result = toolbox_factory(p['tools_context']).call('teams_notify',
                    dict(card_id=card,message=message,request_id=record['request_id']))
            else:
                from ldcon.agents.card_fallback import FallbackStore
                from ldcon.agents.teams_card import enqueue,receipt,parameters
                box=Toolbox(p['tools_context'])
                queue=FallbackStore(box.root/'notifications')
                params=parameters({'parameters':candidates[card]},{'message':message})
                delivery=enqueue(queue,box.meta,card,'agent:'+record['request_id'],params,
                    dict(source='agent_startup',agent_bridge_id=p['bridge_id'],message=message,
                         _request_id=record['request_id'],_notice=dict(event='startup')))
                result=receipt(queue,delivery)
            result = json.loads(result) if isinstance(result, str) else result
            record['status'] = 'notification_queued' if result.get('status') == 'queued' else 'notification_unknown'
            if result.get('delivery_id'):
                record['delivery_id'] = result['delivery_id']
        except Exception as error:
            # Notification failure must not kill a usable diagnostic session.
            # Do not persist raw exception text: a transport may include secrets.
            record.update(status='notification_unknown', error_type=type(error).__name__)
    atomic_json(record_path, record)
    atomic_json(latest, record)
    return record
