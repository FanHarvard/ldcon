"""Offline thermal evidence and real target-card sequencing, no hardware."""
import copy
from types import SimpleNamespace as NS

import pytest

from ldcon.owners import ecdl_target_settling as thermal
from ldcon.core.contracts import DeviceError
from .test_target_sequence import rig, F


def row(stamp, **changes):
    evidence = dict(status='centered', pzt_dac=32000., center_dac=32000.,
        tolerance_count=10., confirmation_s=1., max_age_s=.5, reference_confirmed=True,
        context_revision='center-1', observed_unix_s=stamp, target_frequency_hz=F+100e6)
    evidence.update(changes)
    return dict(ramped_target_hz=F+100e6, tec_centering=evidence)


def poll(completion, stamp, **changes):
    return completion.poll(row(stamp, **changes), accepted_unix_s=0., now=stamp, at_target=True)


def test_one_in_band_sample_or_duplicate_never_means_thermal_settling():
    c = thermal.Completion()
    for now in (1., 1.2, 1.4):
        r = c.poll(row(1.), accepted_unix_s=0., now=now, at_target=True)
        assert r['status'] != 'settled' and r['samples'] == 1
    for stamp in (1.4, 1.8, 2.):
        r = poll(c, stamp)
    assert r['status'] == 'settled' and r['confirmed_s'] == 1.


@pytest.mark.parametrize('changes', [dict(pzt_dac=32010.), dict(pzt_dac=31990.),
    dict(status='paused'), dict(reference_confirmed=False), dict(observed_unix_s=0.),
    dict(observed_unix_s=5.), dict(tolerance_count=float('nan')),
    dict(target_frequency_hz=F), dict(context_revision='new-gain-or-TEC-write')])
def test_excursion_pause_stale_or_context_change_resets_confirmation(changes):
    c = thermal.Completion()
    for stamp in (1., 1.4, 1.8):
        poll(c, stamp)
    assert poll(c, 2., **changes)['status'] != 'settled'
    assert poll(c, 2.2)['status'] == 'confirming'
    assert poll(c, 2.6)['status'] == 'confirming'
    assert poll(c, 3.)['status'] == 'confirming'
    assert poll(c, 3.4)['status'] == 'settled'


def test_sample_gap_or_unsupported_worker_cannot_use_old_success():
    c = thermal.Completion()
    for stamp in (1., 1.4, 1.8, 2.):
        poll(c, stamp)
    assert poll(c, 3.)['status'] == 'confirming'
    assert c.poll({}, accepted_unix_s=0., now=3., at_target=True)['status'] == 'waiting'
    assert poll(c, 3.1)['samples'] == 1


def native():
    policies = dict(piezo=NS(low=0., high=65535., resolution=1.),
                    temperature=NS(resolution=.0001, update_interval_s=2.))
    owner = NS(lease='lease', last_write={'temperature': 0.}, policies=lambda: policies,
               device={'control_settings': {'max_sample_age_s': 2.}})
    n = NS(o=owner, p=dict(tec_center_reference='activation', tec_center_fraction=.5,
        counts_per_temperature_unit=10000., tec_gain=.05, supervision_interval_s=10.),
        tec_reference_dac=32000., tec_reference_lease='lease', tec_reference_confirmed=True,
        tec_ff_paused=False, current_target=F)
    n.planner = NS(p=n.p, policies=policies, tec_reference_dac=n.tec_reference_dac)
    return n


def snapshot():
    return dict(read_started_unix_s=1., outputs=dict(piezo=32000.),
                servo_config=dict(target=299792458e9/F, pid_multiplier=1))


def test_owner_geometry_matches_existing_TEC_gain_and_output_quantization():
    n = native()
    state = snapshot()
    before = copy.deepcopy(state)
    result = thermal.owner_evidence(n, state, {})
    assert result['tolerance_count'] == 10.
    assert result['confirmation_s'] == 10.
    assert result['center_dac'] == 32000. and result['status'] == 'centered'
    assert state == before
    n.p['tec_center_reference'] = 'range_midpoint'
    assert thermal.owner_evidence(n, state, {})['center_dac'] == 65535./2
    n.planner.policies = dict(n.planner.policies, piezo=NS(low=30000., high=34000., resolution=1.))
    assert thermal.owner_evidence(n, state, {})['center_dac'] == 32000.


def test_owner_cannot_relabel_pre_write_snapshot_as_new_target_or_pending_TEC_as_settled():
    n = native()
    state = snapshot()
    n.current_target += 100e6
    assert thermal.owner_evidence(n, state, {})['status'] == 'unavailable'
    n.current_target = F
    assert thermal.owner_evidence(n, state, {'actions': [{'axis': 'temperature'}]})['status'] == 'centering'
    n.tec_ff_paused = True
    assert thermal.owner_evidence(n, state, {})['status'] == 'paused'
    n.tec_ff_paused = False
    n.o.lease = 'new-lease'
    assert thermal.owner_evidence(n, state, {})['status'] == 'unavailable'


def ecdl(api):
    api.doc.update(interfaces={'lock': {'device': 'rig'}}, devices={'rig': {'type': 'ecdl.rig.owner'}})


def test_next_step_completes_on_frequency_and_reports_tec_centering(rig):
    """Human decision 2026-10-27 (target-ff design): completion is frequency arrival; TEC centering is a report."""
    api, now = rig
    ecdl(api)

    def observe(value):
        age = now[0] - api.write_times[-1]
        value['tec_centering'] = row(now[0], target_frequency_hz=value['ramped_target_hz'],
            pzt_dac=32040. if age < 2. else 32000.)['tec_centering']
        value['pzt_headroom_hz'] = dict(up_hz=5e9, down_hz=5e9)
        return value

    api.observation = observe
    result = api.run()
    assert len(api.writes) == 3
    # no step waited for the PZT to return to the TEC centre (the old thermal gate needed >= 3 s per step)
    assert all(b-a < 2. for a,b in zip(api.write_times, api.write_times[1:]))
    for step in result['steps']:
        verify = step['verify']
        assert verify['completion_basis'] == 'frequency_arrival' and verify['tec_centering_used_for_completion'] is False
        assert verify['tec_centering']['status'] in ('waiting', 'confirming', 'settled')      # reported, not gating
        assert verify['pzt_headroom_hz'] == dict(up_hz=5e9, down_hz=5e9)
    assert not any(p['status'] == 'waiting_for_tec_center' for p in api.progress)
    assert any(p.get('tec_centering') for p in api.progress)


@pytest.mark.parametrize('evidence', ['missing', 'outside', 'paused'])
def test_incomplete_TEC_return_never_blocks_a_frequency_settled_step(rig, evidence):
    api, now = rig
    ecdl(api)
    if evidence != 'missing':
        def observe(value):
            value['tec_centering'] = row(now[0], pzt_dac=32040. if evidence == 'outside' else 32000.,
                status='paused' if evidence == 'paused' else 'centered')['tec_centering']
            return value
        api.observation = observe
    result = api.run(timeout_s=5.)
    assert api.writes == [F+100e6, F+200e6, F+250e6]
    reported = [s['verify'].get('tec_centering') for s in result['steps']]
    assert all(r['status'] != 'settled' for r in reported)
    if evidence == 'missing':
        assert all(r['status'] == 'waiting' for r in reported)


def test_learned_frequency_profile_completes_without_thermal_wait(rig, monkeypatch):
    api, now = rig
    ecdl(api)
    import ldcon.jobs.settling_profile as empirical
    class FastProfile:
        enabled = True
        def __init__(self, *args, **kwargs): pass
        def poll(self, now): return dict(status='settled')
    monkeypatch.setattr(empirical, 'Completion', FastProfile)
    result = api.run(timeout_s=2., queue_steps=False, target_frequency_hz=F+100e6)
    assert api.writes == [F+100e6] and result['verify']['ok'] and result['verify']['tec_centering_used_for_completion'] is False


def test_public_target_API_keeps_thermal_wait_nonterminal_and_client_compatible():
    """Old lock workers may still publish waiting_for_tec_center; the API maps it to verifying (contract unchanged)."""
    from ldcon.web.lock_target_api import LockTargetApi
    api = object.__new__(LockTargetApi)
    api.runtime = NS(get_job=lambda _: dict(status='running', lock_target=dict(
        status='waiting_for_tec_center', tec_centering=dict(reason='PZT is not centered'))))
    state = api._state('laser', {'change_id': 'id'})
    assert state['state'] == 'verifying' and state['final'] is False
    assert 'PZT is not centered' in state['message']


def test_frequency_still_has_to_pass_after_TEC_returns(rig):
    api, now = rig
    ecdl(api)
    def observe(value):
        value['tec_centering'] = row(now[0])['tec_centering']
        value['frequency_hz'] += 2e6
        return value
    api.observation = observe
    with pytest.raises(DeviceError, match='deadline reached'):
        api.run(timeout_s=2., tolerance_hz=1e6)
    assert api.writes == [F+100e6]
