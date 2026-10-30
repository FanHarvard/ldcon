"""Startup-only native laser assembly construction; no request-selected wiring."""
import copy

from experiment_core.records import digest


def make_assembly_factory(laser):
    from ldcon.jobs.native_ff_sweep import configuration, NativeFFAwg
    from ldcon.owners.native_system_owner import assembly_wiring
    laser = copy.deepcopy(laser)
    config = configuration(laser)
    native = laser['devices'][laser['interfaces']['native_current']['device']]
    expected_policy = native['control_settings']['native_current']
    if 'native_ff_sweep' not in native['control_settings']:
        raise ValueError('Native assembly requires an FF profile in the laser definition')
    expected_device = native['connection']['device_id']
    if set(config['broker'].get('projection', {})) != {'principal', 'key_env'}:
        raise ValueError('Native assembly requires an explicit system projection signing binding')

    def factory(system_owner, session_request):
        from ldcon.owners.native_policy_reload import changes
        from ldcon.owners.native_policy_update import LIVE_ALLOWED
        from ldcon.owners.native_ff_proof import validate_profile
        current_policy = system_owner.policy
        # Startup requires an exact source match for the physical/wiring part. Later sessions follow the system
        # owner's adopted operating policy and FF profile (rule M step 4: the profile is adopted live through
        # policy_update with the validate_profile proof); assembly calibration and wiring stay pinned.
        if (set(changes(expected_policy, current_policy)) - LIVE_ALLOWED or system_owner.ff_profile is None
                or system_owner.runtime.driver.device_id != expected_device):
            raise ValueError('Native assembly configuration differs from the effective laser owner')
        validate_profile(system_owner.ff_profile, current_policy)
        return NativeFFAwg(copy.deepcopy(config), authority=system_owner.authority,
                           journal=system_owner.runtime._report_assembly_event)
    # The dependency pins the wiring only; the operating policy and the target are layer 2 (hot).
    factory.definition_revision = digest(assembly_wiring(laser))
    factory.laser_snapshot = copy.deepcopy(laser)
    return factory
