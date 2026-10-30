"""Revision-checked computation profiles, separate from laser authority."""
import copy
import fcntl
import json
import os
from pathlib import Path
from ldcon.core.paths import resources_dir

from experiment_core.agent_contracts import execution_profile
from experiment_core.records import atomic_json, digest, safe_id


EMPTY = dict(schema_version=1, profiles={}, resident_profile='', lasers={})


def location(root):
    configured = os.environ.get('LDCON_AGENT_PROFILE_REGISTRY')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError('Agent profile registry must be absolute')
        return path
    root = Path(root).resolve()
    if root.parent.name in ('deployments', 'lasers'):
        return root.parent.parent / 'dashboard' / 'agent-profiles.json'
    if root.name == 'dashboard':
        return root / 'agent-profiles.json'
    return root / 'agents' / 'execution-profiles.json'


def model_options():
    # Reviewed capability snapshot: no inference, login or hardware request on a page load.
    path=resources_dir()/'agent-model-options.json'
    return json.loads(path.read_text())


def validate_selection(value, previous):
    options=model_options()['providers']
    for name,profile in value['profiles'].items():
        old=previous.get('profiles',{}).get(name,{})
        if all(profile.get(k)==old.get(k) for k in ('provider','model','effort')):continue
        model=next((m for m in options.get(profile['provider'],[]) if m['model']==profile['model']),None)
        if model is None or profile.get('effort') and profile['effort'] not in model['efforts']:
            raise ValueError('Choose a documented model and effort from the provider catalog')


def validate(value):
    from ldcon.agents.agent_providers import PROVIDERS
    if not isinstance(value, dict) or set(value) != set(EMPTY) or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid execution profile registry')
    result = copy.deepcopy(value)
    if not isinstance(value['profiles'], dict) or len(value['profiles']) > 64:
        raise ValueError('Expected at most 64 profiles')
    for name, profile in value['profiles'].items():
        safe_id(name)
        result['profiles'][name] = execution_profile(profile)
        if profile['provider'] not in PROVIDERS:
            raise ValueError('Unknown execution provider')
    if not isinstance(value['lasers'], dict) or len(value['lasers']) > 1024:
        raise ValueError('Invalid per-laser execution defaults')
    for name, setting in value['lasers'].items():
        safe_id(name)
        if not isinstance(setting, dict) or set(setting) != {'resident_profile'}:
            raise ValueError('Invalid per-laser execution default')
    for selected in [value['resident_profile'], *(v['resident_profile'] for v in value['lasers'].values())]:
        if not isinstance(selected,str):raise ValueError('Invalid execution default')
        if selected and selected not in result['profiles']:
            raise ValueError('Execution default references an absent profile')
    return result


def read(root):
    path = location(root)
    if not path.exists():
        return dict(document=copy.deepcopy(EMPTY), revision=None)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError('Invalid execution profile file')
    value = validate(json.loads(path.read_text()))
    return dict(document=value, revision=digest(value))


def save(root, value, expected_revision, *, catalog_required=False):
    value = validate(value)
    path = location(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path=path.with_suffix('.lock')
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError('Invalid execution profile lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read(root)['revision'] != expected_revision:
            from ldcon.web.web_runtime import ConflictError
            raise ConflictError('Agent profiles changed; reload before saving')
        if catalog_required:validate_selection(value,read(root)['document'])
        from ldcon.agents.agent_routing import location as routing_location, validate as validate_routing
        route_path=routing_location(root)
        if route_path.exists():validate_routing(json.loads(route_path.read_text()),value['profiles'])
        atomic_json(path, value)
    return read(root)


def resolve(root, name='', *, laser_id='', resident=False):
    record = read(root)
    document = record['document']
    if not name and resident:
        from ldcon.agents.agent_routing import gateway_selection
        route=gateway_selection(root,laser_id)
        name = route['profile'] if route else document['lasers'].get(laser_id, {}).get('resident_profile', document['resident_profile'])
    if not name:
        return None
    if name not in document['profiles']:
        raise ValueError('Execution profile is not configured: ' + str(name))
    profile = copy.deepcopy(document['profiles'][name])
    return dict(id=name, registry_revision=record['revision'], profile_revision=digest(profile), **profile)


def task_policy(card, parameters):
    from experiment_core.agent_contracts import execution_policy
    return execution_policy(card.get('agent_execution', dict(mode='required', task_profile=parameters['execution_profile'])))


def select_task(root, card, parameters, *, meta, deck=None, invocations=None):
    """Pin computation to the chosen gateway's harness, never its model or authority."""
    from experiment_core.agent_contracts import choose_profile
    policy=task_policy(card,parameters)
    provider='';origin='explicit_profile' if parameters.get('execution_profile') else 'fixed_card_profile'
    gateway=parameters.get('agent_card_id','')
    inherited=bool(policy['provider_profiles'] or policy.get('difficulty'))
    routing=None
    if policy.get('difficulty'):
        from ldcon.agents.agent_routing import read as routing_read
        routing=routing_read(root)
        if not routing['configured']:raise ValueError('Configure the agent difficulty routing table')
    if inherited and not parameters.get('execution_profile'):
        import sqlite3
        database=Path(root)/'agents'/'agents.sqlite3'
        key=json.dumps([meta['set_id'],meta['deck_id'],gateway],separators=(',',':'))
        row=None
        if database.is_file():
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                row=db.execute('SELECT provider,status FROM sessions WHERE key=?',(key,)).fetchone()
        if row and row[1] in ('ready','busy'):
            provider=row[0];origin='connected_gateway'
        else:
            if deck is None:
                from ldcon.core.interface_schema import InterfaceRepository
                bundle=InterfaceRepository(root).assignment(meta['set_id'])
                definition=next(d for d in bundle['deck_definitions'] if d['id']==meta['deck_id'])
                deck=definition['document'];invocations=definition['invocation_parameters']
            resident=next((c for c in deck['cards'] if c['id']==gateway and c['operation']=='agent.resident'),None)
            if resident is None:raise ValueError('Select a configured gateway card for inherited task execution')
            p={**resident.get('parameters',{}),**(invocations or {}).get(gateway,{})}
            saved=resolve(root,p.get('execution_profile',''),laser_id=meta.get('laser_id',meta['set_id']),resident=True)
            provider=saved['provider'] if saved else p.get('provider','claude')
            origin='configured_gateway'
    name=choose_profile(policy,requested=parameters.get('request_delegation',True),
                        override=parameters.get('execution_profile',''),provider=provider,
                        matrix=routing['document']['provider_profiles'] if routing else None,
                        default_difficulty=routing['document']['default_difficulty'] if routing else 'standard')
    selected=resolve(root,name) if name else None
    if selected and inherited and not parameters.get('execution_profile') and selected['provider']!=provider:
        raise ValueError('Card provider profile does not match the selected gateway interface')
    return selected,dict(origin=origin,gateway_card_id=gateway,gateway_provider=provider,
                         task_provider=selected['provider'] if selected else None,
                         difficulty=(routing['document']['default_difficulty'] if policy['difficulty']=='default' else policy['difficulty']) if routing else None,
                         routing_snapshot=copy.deepcopy(routing['document']) if routing else None,
                         routing_revision=routing['revision'] if routing else None)


def install_routes(app, prefix, body, *, root=None):
    from fastapi import Request
    from ldcon.agents.agent_routing import install_routes as routing_routes
    routing_routes(app,prefix,body,root=root)
    def settings_root():return root if root is not None else app.state.runtime.root
    @app.get(prefix + '/api/agent-profiles')
    def profiles():
        from ldcon.agents.agent_providers import catalog
        return {**read(settings_root()), 'providers': catalog(), 'model_options': model_options(),
                'activation': 'New sessions/tasks only; current runs pin their profile.'}

    @app.put(prefix + '/api/agent-profiles')
    async def profiles_save(request: Request):
        value = await body(request)
        if set(value) != {'document', 'expected_revision'}:
            raise ValueError('Expected document and expected_revision')
        return save(settings_root(), value['document'], value['expected_revision'], catalog_required=True)
