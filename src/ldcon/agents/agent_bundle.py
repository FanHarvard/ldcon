"""Installed English instruction sources and bounded reproducible task input."""
import hashlib
import json
from pathlib import Path
from ldcon.core.paths import resources_dir
from experiment_core.agent_contracts import instruction_bundle
from experiment_core.records import atomic_json, digest

REVISION = '1.7'
PROFILES = {'diagnosis', 'adjustment', 'research'}


def build(directory, profile, *, context, evidence, max_input_bytes=32000):
    if profile not in PROFILES:
        raise ValueError('Unknown instruction profile')
    resources = resources_dir() / 'agent_contract'
    from ldcon.core.records import read_json
    context = {**context, 'operating_mode_at_card_start':
               read_json(Path(directory)/'operating-mode-at-start.json',
                         dict(status='unavailable', reason='No owner observation recorded at card start'))}
    from experiment_core.adjustment_guidance import instructions as guidance_instructions
    context['adjustment_guidance_at_card_start']=read_json(
        Path(directory)/'adjustment-guidance-at-start.json',
        dict(availability='not_recorded',advisory_only=True))
    from ldcon.agents.report_pdf import instructions as report_instructions
    bundle = instruction_bundle((resources/'common.txt').read_bytes()+b'\n'+(resources/'response_optimization.txt').read_bytes()+b'\n'+guidance_instructions().encode()+b'\n'+report_instructions().encode(),
        (resources/(profile+'.txt')).read_bytes(), revision=REVISION,
        context=context, evidence=evidence, max_input_bytes=max_input_bytes)
    bundle['instruction_profile'] = profile
    bundle['bundle_revision'] = digest(bundle)
    atomic_json(Path(directory)/'agent-bundle.json', bundle)
    return bundle


def turn_origin(directory, request_id, kind, *, active=True, task_id=None):
    # The inbox wrapper is host-created. Its messages themselves still grant no
    # physical authority; language selection is not authorization.
    from ldcon.core.records import atomic_json
    value = dict(request_id=request_id, kind=kind, active=active,
                 job_id=Path(directory).name,
                 origin='human' if kind in ('request', 'hub_inbox_watch') else 'automatic')
    if task_id:
        value['task_id'] = task_id
    atomic_json(Path(directory)/'agent-current-request.json', value)


def capability_probe(box, args):
    """Local, non-actuating end-to-end MCP probe with an unpredictable challenge."""
    from ldcon.core.records import read_json
    from ldcon.agents.agent_tools import TOOLS
    challenge = read_json(box.directory/'agent-probe-challenge.json', {})
    if args.get('nonce') != challenge.get('nonce') or not challenge:
        raise ValueError('Capability probe challenge changed')
    manifest = read_json(box.directory/'agent-bundle.json', {})
    if not manifest or manifest['bundle_revision'] != challenge['bundle_revision']:
        raise ValueError('Capability probe bundle mismatch')
    if digest({k:v for k,v in manifest.items() if k!='bundle_revision'})!=manifest['bundle_revision']:
        raise ValueError('Capability probe bundle integrity mismatch')
    if (hashlib.sha256(manifest['instructions'].encode()).hexdigest()!=manifest['instructions_sha256']
            or digest(manifest['input'])!=manifest['input_sha256']):
        raise ValueError('Capability probe content integrity mismatch')
    if manifest['input']['context']['laser_id']!=box._laser_id:
        raise ValueError('Capability probe laser mismatch')
    names = {item['name'] for item in TOOLS}
    allowed = box.context.get('task_allowed_tools')
    if allowed is not None:
        names &= set(allowed)
    receipt = dict(nonce=challenge['nonce'], bundle_revision=manifest['bundle_revision'],
                   laser_id=box._laser_id, tools=sorted(names),
                   scoped_bundle_read=True, hardware_operations=0,
                   probe_type='mcp_probe_invocation_and_scoped_manifest_read',
                   tool_listing='enumerated', operation_permissions='not_probed',
                   image_interpretation='not_probed')
    atomic_json(box.directory/'agent-probe-receipt.json', receipt)
    return receipt
