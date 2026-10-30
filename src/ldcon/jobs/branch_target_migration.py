"""Pure, reviewable migration; no services, owners or hardware are contacted."""
import copy

from ldcon.core.interface_schema import digest
from ldcon.core.target_policy import laser_target, validate_transition


def prepare(document, *, recorded_unix_s):
    candidate = copy.deepcopy(document)
    target = laser_target(candidate)
    if target and target.get('bounds_hz') is not None:
        lo, hi = target.pop('bounds_hz')
        target['allowed_change_hz'] = [lo-target['frequency_hz'], hi-target['frequency_hz']]
    annotations = []
    for key, device in candidate.get('devices', {}).items():
        if device.get('type') != 'ecdl.rig.owner':
            continue
        settings = device['control_settings']
        optical = settings.get('optical', {})
        gate = settings.get('steady_lock', {}).get('entry_gate', {})
        bands = optical.pop('mode_bands', None)
        old_gate = {k:gate.pop(k) for k in ('mode_id', 'branch_tolerance_hz') if k in gate}
        if bands is not None or old_gate:
            annotations.append(dict(device=key, legacy_mode_bands=bands, legacy_entry_fields=old_gate,
                                    measured=False, lock_admission=False))
        # Independent step limits are removed only for TEC. The slew, physical
        # bounds, calibration and normal hardware update cadence are unchanged.
        device['absolute_conditions']['axes']['temperature'].pop('max_step', None)
    validate_transition(document, candidate)
    return candidate, dict(schema='ldcon.legacy-branch-annotations.v1',
        laser_id=document['id'], recorded_unix_s=recorded_unix_s,
        source_configuration_sha256=digest(document),
        note='Unmeasured legacy labels; not a current-sweep map or lock permission', annotations=annotations)
