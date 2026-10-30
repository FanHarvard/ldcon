"""Explicit trusted system bindings for offline shared-session fixtures."""
from ldcon.owner_projection import SystemPolicyAuthority


def bind_sibling(hub, owner):
    policy=dict(channels={'2':dict(bounds=[-2.,2.],axis='native_ff')},max_points=32768,
                max_sample_rate_hz=2000.,writes_enabled=True,cleanup_operations=[])
    authority=SystemPolicyAuthority('sr1092-fixture',policy,dependencies={'fixture':'shared-sdg-v1'},
                                    journal=owner.policy_journal)
    return hub.bind_shared_awg('repump_1092',authority,authority.snapshot(),[2])
