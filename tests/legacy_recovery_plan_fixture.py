# Test-only archive. Never packaged, registered, or used by active recovery.
"""Build a bounded replay proposal from recorded discovery, without device I/O."""
import copy

from ldcon.jobs.ecdl_mode_map import finite, seal, selected_reference


SCHEMA = 'ldcon.ecdl-recovery-plan.v1'
CONTEXT_MODES = ('restore', 'translate')
# context_mode (human decision 2026-10-27): 'restore' moves TEC/PZT back to the map context before the replay (the
# original behaviour, kept as the default of this plan-only module so build_plan callers such as the operating-point
# card are unchanged); 'translate' keeps the present TEC/PZT when they lie within context_tolerances of the map
# context, shifts the current recipe by current_per_tec_v x dTEC and verifies against the predicted frequency
# (reference + temperature slope x dTEC + piezo_hz_per_count x dPZT). The merged recovery card defaults to 'translate'.
# current_per_tec_v (drift prior) convention, shared with tune.ecdl_operating_point ldc_ff (its default):
#   I_predicted = I0 + prior x dTEC, dTEC = TEC now - TEC at the reference (current axis units per TEC volt).
# Default -0.07: sr422 measurement 2026-10-27 (+0.007 V current per -0.1 V TEC, preparations/sr422-relock-20261027/LOG.md).
DEFAULTS = dict(map_artifact=None, map_ref=None, selected_branch_id=None,
                selection_author='automatic', tolerance_hz=None,
                hop_threshold_hz=None, stable_s=5., confirm_samples=3,
                metric_limits={}, timeout_s=600., context_mode='restore',
                context_tolerances={'temperature': .2, 'piezo': 10000.},
                current_per_tec_v=-.07, piezo_hz_per_count=None)


def validate_translation(p):
    if p['context_mode'] not in CONTEXT_MODES:
        raise ValueError('context_mode must be restore or translate')
    tolerances = p['context_tolerances']
    if (not isinstance(tolerances, dict) or set(tolerances) != {'temperature', 'piezo'}
            or any(not finite(v) or v < 0 for v in tolerances.values())):
        raise ValueError('context_tolerances require finite non-negative temperature and piezo values (axis units)')
    if not finite(p['current_per_tec_v']):
        raise ValueError('current_per_tec_v must be finite (current axis units per TEC volt; I = I0 + prior x dTEC)')
    if p['piezo_hz_per_count'] is not None and not finite(p['piezo_hz_per_count']):
        raise ValueError('piezo_hz_per_count must be finite or null (derived from the laser profile)')


def piezo_hz_per_count_from_profile(laser, frequency_hz):
    """steady_lock.wavelength_nm_per_count of the laser's lock device, converted at frequency_hz (df = -f^2/c dlambda)."""
    from ldcon.jobs.operating_point import lock_device
    settings = (lock_device(laser)[2].get('control_settings') or {}).get('steady_lock') or {}
    slope = settings.get('wavelength_nm_per_count')
    if not finite(slope) or not finite(frequency_hz) or frequency_hz <= 0:
        raise ValueError('piezo_hz_per_count is required (no finite steady_lock.wavelength_nm_per_count in the laser profile)')
    return -(frequency_hz ** 2 / 299792458.) * slope * 1e-9


def parameters(card, overrides):
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown recovery plan parameter')
    validate_translation(p)
    if p['map_artifact'] is not None and not isinstance(p['map_artifact'], dict):
        raise ValueError('Expected mode map artifact')
    validate_reference(p['map_ref'])
    if p['selected_branch_id'] is not None and not isinstance(p['selected_branch_id'], str):
        raise ValueError('Expected mapped branch id')
    if p['selection_author'] not in ('automatic', 'human', 'agent'):
        raise ValueError('Unknown branch selection author')
    for name in ('tolerance_hz', 'hop_threshold_hz', 'stable_s', 'timeout_s'):
        if p[name] is None and name in ('tolerance_hz', 'hop_threshold_hz'):
            continue
        if not finite(p[name]) or p[name] <= 0:
            raise ValueError('Positive ' + name + ' required')
    if p['timeout_s'] > 86400 or p['stable_s'] >= p['timeout_s']:
        raise ValueError('Invalid finite verification budget')
    if type(p['confirm_samples']) is not int or not 3 <= p['confirm_samples'] <= 100:
        raise ValueError('At least three fresh confirmation samples required')
    limits = p['metric_limits']
    if not isinstance(limits, dict) or any(not isinstance(k, str) or not finite(v) or v < 0 for k, v in limits.items()):
        raise ValueError('Invalid optical metric limits')
    return p


def validate_reference(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'job_id', 'sha256'}
            or not isinstance(value['job_id'], str) or not value['job_id']
            or not isinstance(value['sha256'], str) or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise ValueError('Artifact reference requires job_id and SHA256')


def build_plan(p):
    p = parameters(dict(parameters=p), {})
    if any(p[k] is None for k in ('map_artifact', 'tolerance_hz', 'hop_threshold_hz')):
        raise ValueError('Map artifact and explicit frequency verification/hop tolerances required')
    if p['context_mode'] == 'translate' and p['piezo_hz_per_count'] is None:
        raise ValueError('context_mode translate requires piezo_hz_per_count (parameter or laser profile)')
    mapping, branch, reference = selected_reference(p['map_artifact'], p['selected_branch_id'])
    policy = mapping['policies']['current']
    span = policy['high'] - policy['low']
    prefix = mapping['path'][:reference['index'] + 1]
    if not prefix:
        raise ValueError('Selected branch has no discovery path')
    # Preparation and conditioning are preserved separately from directed scan.
    recipe = [dict(action='restore_context', axis='temperature', value=reference['outputs']['temperature']),
              dict(action='restore_context', axis='piezo', value=reference['outputs']['piezo'])]
    recipe.extend(dict(action='conditioning', axis='current', value=policy['low'] + f * span)
                  for f in mapping['conditioning_fractions'])
    recipe.append(dict(action='preparation', axis='current', value=prefix[0]))
    recipe.extend(dict(action='directed_scan', axis='current', value=value,
                       dwell_s=mapping['points'][index]['dwell_s']) for index, value in enumerate(prefix))
    return seal(dict(schema=SCHEMA, map_sha256=mapping['artifact_sha256'],
                     branch_id=branch['id'], selection_author=p['selection_author'],
                     selected_reference=reference, policies=mapping['policies'],
                     direction='up' if mapping['path'][-1] > mapping['path'][0] else 'down',
                     recipe=recipe, source_initial_context=mapping['initial'],
                     verification=dict(tolerance_hz=p['tolerance_hz'], hop_threshold_hz=p['hop_threshold_hz'],
                                       stable_s=p['stable_s'], confirm_samples=p['confirm_samples'],
                                       metric_limits=p['metric_limits'], timeout_s=p['timeout_s']),
                     context_mode=p['context_mode'], context_tolerances=copy.deepcopy(p['context_tolerances']),
                     current_per_tec_v=p['current_per_tec_v'], piezo_hz_per_count=p['piezo_hz_per_count'],
                     verified=False, hop_cause='unknown',
                     qualification='Recorded discovery replay proposal; actual induced-hop recovery not yet tested'))


def run_recovery_plan(ports, p, *, emit, **arguments):
    """Common card runner signature; ports intentionally unused."""
    artifact = build_plan(p)
    emit('ecdl_recovery_plan_artifact', dict(schema=SCHEMA,
        artifact_sha256=artifact['artifact_sha256'], map_sha256=artifact['map_sha256'],
        branch_id=artifact['branch_id'], recipe_steps=len(artifact['recipe']), verified=False))
    return dict(status='succeeded', artifact=artifact, verified=False,
                branch_id=artifact['branch_id'], hardware_operations=0)
