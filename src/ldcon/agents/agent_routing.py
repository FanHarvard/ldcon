"""Explicit computation routing, separate from profile definitions and laser authority."""
import copy
import fcntl
import json
from experiment_core.agent_contracts import DIFFICULTIES, difficulty_profile
from experiment_core.records import atomic_json, digest, safe_id

EMPTY = dict(schema_version=1, default_difficulty='standard', provider_profiles={},
             gateway=dict(provider='claude', difficulty='basic'), lasers={})


def location(root):
    from ldcon.agents.agent_profiles import location as profiles_location
    return profiles_location(root).with_name('agent-routing.json')


def validate(document, profiles):
    if not isinstance(document, dict) or set(document)!=set(EMPTY) or type(document['schema_version']) is not int or document['schema_version']!=1:
        raise ValueError('Invalid agent routing table')
    if document['default_difficulty'] not in DIFFICULTIES:
        raise ValueError('Invalid default difficulty')
    matrix=document['provider_profiles']
    if not isinstance(matrix,dict) or len(matrix)>16:
        raise ValueError('Invalid provider difficulty matrix')
    for provider, levels in matrix.items():
        safe_id(provider)
        if not isinstance(levels,dict) or set(levels)!=set(DIFFICULTIES):
            raise ValueError('Configure Basic, Standard and Deep for each enabled interface')
        for name in levels.values():
            safe_id(name)
            if name not in profiles or profiles[name]['provider']!=provider:
                raise ValueError('Routing profile is absent or belongs to another provider: '+name)
    if not isinstance(document['lasers'],dict) or len(document['lasers'])>1024:
        raise ValueError('Invalid laser gateway defaults')
    for name in document['lasers']:safe_id(name)
    for selection in [document['gateway'],*document['lasers'].values()]:
        if not isinstance(selection,dict) or set(selection)!={'provider','difficulty'}:
            raise ValueError('Gateway defaults require provider and difficulty')
        difficulty_profile(matrix,selection['provider'],selection['difficulty'])
    return copy.deepcopy(document)


def read(root):
    path=location(root)
    if not path.exists():return dict(document=copy.deepcopy(EMPTY),revision=None,configured=False)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>262144:
        raise ValueError('Invalid agent routing file')
    from ldcon.agents.agent_profiles import read as read_profiles
    value=validate(json.loads(path.read_text()),read_profiles(root)['document']['profiles'])
    return dict(document=value,revision=digest(value),configured=True)


def gateway_selection(root, laser_id=''):
    record=read(root)
    if not record['configured']:return None
    document=record['document'];selection=document['lasers'].get(laser_id,document['gateway'])
    name=difficulty_profile(document['provider_profiles'],selection['provider'],selection['difficulty'])
    return dict(profile=name,**selection,routing_revision=record['revision'])


def save(root,document,expected_revision):
    from ldcon.agents.agent_profiles import read as read_profiles, location as profile_location
    path=location(root);path.parent.mkdir(parents=True,exist_ok=True)
    # Registry and routing writes use the same lock: a referenced profile cannot
    # disappear between validation and publication.
    lock_path=profile_location(root).with_suffix('.lock')
    if lock_path.is_symlink():raise ValueError('Invalid profile lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=validate(document,read_profiles(root)['document']['profiles'])
        if read(root)['revision']!=expected_revision:
            from ldcon.web.web_runtime import ConflictError
            raise ConflictError('Agent routing changed; reload before saving')
        atomic_json(path,value)
    return read(root)


def install_routes(app,prefix,body,*,root=None):
    from fastapi import Request
    def settings_root():return root if root is not None else app.state.runtime.root

    @app.get(prefix+'/api/agent-routing')
    def routing():
        return read(settings_root())

    @app.put(prefix+'/api/agent-routing')
    async def routing_save(request:Request):
        value=await body(request)
        if set(value)!={'document','expected_revision'}:raise ValueError('Expected document and expected_revision')
        return save(settings_root(),value['document'],value['expected_revision'])
