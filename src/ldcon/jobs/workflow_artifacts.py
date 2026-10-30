"""Resolve immutable, same-laser workflow evidence without opening devices."""
import copy
import json
import re

from ldcon.jobs.ecdl_mode_map import checked_artifact


KINDS = {'map':('mode-map.json','ldcon.ecdl-mode-map.v1'),
         # ONE tested recovery artifact (prepare.ecdl_recovery, 2026-10-27): steady lock recovery_ref, recover card
         'recovery':('recovery.json','ldcon.ecdl-recovery.v2'),
         # deprecated pair of the retired plan.ecdl_recovery / test.ecdl_recovery cards (old artifacts only)
         'plan':('recovery-plan.json','ldcon.ecdl-recovery-plan.v1'),
         'recovery_plan':('recovery-plan.json','ldcon.ecdl-recovery-plan.v1'),
         'recovery_test':('recovery-test.json','ldcon.ecdl-recovery-test.v1')}


def resolve_inputs(runtime, laser_id, parameters):
    result = copy.deepcopy(parameters)
    for kind, (name, schema) in KINDS.items():
        ref = result.get(kind + '_ref')
        if ref is None:
            continue
        if (not isinstance(ref, dict) or set(ref) != {'job_id', 'sha256'}
                or not isinstance(ref['job_id'], str)
                or not isinstance(ref['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
            raise ValueError('Artifact reference requires job_id and SHA256')
        job = runtime.get_job(ref['job_id'])
        if job.get('laser', {}).get('id') != laser_id or job['status'] != 'succeeded':
            raise ValueError('Artifact must belong to a successful job of this laser')
        path = runtime.directory(ref['job_id']) / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Workflow artifact is unavailable or too large')
        artifact = checked_artifact(json.loads(path.read_text()), schema)
        if artifact['artifact_sha256'] != ref['sha256']:
            raise ValueError('Workflow artifact changed; reload its reference')
        if result.get(kind + '_artifact') not in (None, artifact):
            raise ValueError('Inline artifact differs from the referenced job')
        result[kind + '_artifact'] = artifact
    return result


def freeze_inputs(directory, parameters):
    """Keep potentially large raw patterns outside job metadata and invocation."""
    from ldcon.core.records import atomic_json
    result = copy.deepcopy(parameters)
    manifest = {}
    for kind, (_, schema) in KINDS.items():
        artifact = result.get(kind + '_artifact')
        if artifact is None:
            continue
        artifact = checked_artifact(artifact, schema)
        atomic_json(directory / ('input-' + kind + '.json'), artifact)
        manifest[kind] = artifact['artifact_sha256']
        result[kind + '_artifact'] = None
    return result, manifest


def hydrate_inputs(directory, parameters, manifest):
    """Load only fixed, hashed input names, before any hardware interfaces open."""
    result = copy.deepcopy(parameters)
    if not isinstance(manifest, dict) or set(manifest) - KINDS.keys():
        raise ValueError('Invalid frozen workflow input manifest')
    for kind, expected in manifest.items():
        path = directory / ('input-' + kind + '.json')
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Frozen workflow artifact unavailable or too large')
        artifact = checked_artifact(json.loads(path.read_text()), KINDS[kind][1])
        if artifact['artifact_sha256'] != expected:
            raise ValueError('Frozen workflow artifact hash mismatch')
        result[kind + '_artifact'] = artifact
    return result
