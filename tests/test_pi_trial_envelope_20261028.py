"""2026-10-28 (sr422, task c1d5f6e7): background PI trials and the runtime-adjustment capability report.

(1) A factor-reduction trial of an already-commissioned native PI is bounded by the APPROVED commissioned correction
    (commissioned kp * capture_nm + integral_limit_count), not by commissioning.max_correction_count, which bounds
    only the first stage of commissioning an unverified PI (design 2026-10-22). sr422: i20000 / 1200 -> admitted.
(2) The adjustment-task gateway reports the runtime envelope the rig owner has ADOPTED, not the controller job's
    frozen laser.yaml (a live-adopted feedforward bound change, 2026-10-28 02:32 EDT, was not shown).
"""
import copy
import json
from pathlib import Path
import sys

import pytest
import yaml

from ldcon.core.contracts import DeviceError
from ldcon.jobs.online_pid import parameters as online_parameters
from ldcon.owners import ecdl_native_pid
from ldcon.owners.ecdl_native_pid import C_NM, NativePIDTuning, correction_envelope

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling fixtures (importlib import mode)

TARGET = 710962838507871.5          # sr422 science target
INTERVAL = .084                     # native_interval_reference_s


def profile(**changes):
    p = dict(p_sample=.1, g_sample=.5, wavelength_nm_per_count=-5.4e-08, capture_hz=1e8, integral_limit_count=20000.,
             commissioning=dict(max_correction_count=1200.), handover_evidence='pi-commission-fixture.json',
             online_pid=dict(enabled=True, max_step_count=10., settle_s=3., max_sample_gap_s=3., minimum_gain_factor=.25))
    p.update(changes)
    return p


def commissioned_config(p):
    slope = abs(p['wavelength_nm_per_count'])
    ki = p['g_sample'] / (INTERVAL * slope)
    return dict(pid_kp=p['p_sample'] / slope, pid_ki=ki, pid_kd=0., control_offset=30000.,
                pid_integral_min=-p['integral_limit_count'] / ki, pid_integral_max=p['integral_limit_count'] / ki,
                system_min=C_NM / (TARGET + p['capture_hz']), system_max=C_NM / (TARGET - p['capture_hz']),
                target=C_NM / TARGET)


class Owner:
    def __init__(self, tmp_path):
        self.device = dict(type='ecdl.rig.owner', revision='fixture')
        self.audit_path = tmp_path / 'audit.jsonl'
        self.laser_file = None
        self.records = []

    def clock(self):
        return 100.

    def audit(self, kind, **values):
        self.records.append((kind, values))


class Native:
    def __init__(self, tmp_path, p, config=None):
        self.o = Owner(tmp_path); self.p = p; self.interval = INTERVAL
        self.target = self.current_target = TARGET
        self.study = None; self.commissioning = False
        self.config = config if config is not None else commissioned_config(p)

    def read_feedback(self):
        return dict(servo_config=copy.deepcopy(self.config), outputs=dict(current=.1, temperature=.5)), {}

    def native(self, state, obs, p, locked):
        pass

    def discard_proposals(self, reason):
        pass


@pytest.fixture(autouse=True)
def ready(monkeypatch):
    monkeypatch.setattr(ecdl_native_pid, 'adjustment_ready', lambda obs, p, target: True)


def trial_parameters(factors=(.7, .5)):
    return online_parameters(dict(parameters={}), dict(gain_factors=list(factors), save_settings=False))


def test_commissioned_i20000_profile_admits_factor_reduction_trial(tmp_path):
    native = Native(tmp_path, profile())
    trial = NativePIDTuning(native, trial_parameters())
    envelope = trial.envelope
    p_counts = commissioned_config(native.p)['pid_kp'] * trial.capture_nm
    assert 100 < p_counts < 120                                       # ~110 counts of P over the 100 MHz capture
    assert envelope['envelope_count'] == pytest.approx(p_counts + 20000.)
    assert envelope['envelope_count'] > native.p['commissioning']['max_correction_count']   # refused before 2026-10-28
    assert envelope['commissioned_total_count'] == pytest.approx(envelope['envelope_count'], rel=1e-9)
    assert envelope['trial_total_count'] == pytest.approx(.7 * envelope['commissioned_total_count'])
    assert envelope['trial_totals_count']['0.5'] == pytest.approx(.5 * envelope['commissioned_total_count'])
    started = [values for kind, values in native.o.records if kind == 'native_pid_tuning_started']
    assert started and started[0]['correction_envelope'] == envelope and started[0]['raw_integral_bounds_fixed'] is True
    # Raw bounds stay fixed; the scaled integral limit is the commissioned one at factor 1.
    assert trial.status()['integral_limit_count'] == pytest.approx(20000.)


def test_trial_total_above_approved_envelope_is_refused():
    kp, ki, capture, raw = 1.85e6, 2.2e8, 6e-5, 20000. / 2.2e8
    assert correction_envelope(kp, capture, 20000., kp, ki, raw, [.7, .5])['trial_total_count'] < 20111
    with pytest.raises(DeviceError, match='exceeds approved commissioned correction envelope'):
        correction_envelope(kp, capture, 20000., kp, ki, raw, [1.5])
    # A baseline larger than the approved correction (e.g. a raw bound wider than integral_limit_count) is refused.
    with pytest.raises(DeviceError, match='commissioned PI exceeds'):
        correction_envelope(kp, capture, 20000., kp, ki, 2 * raw, [.5])
    with pytest.raises(DeviceError, match='Invalid commissioned PI correction envelope'):
        correction_envelope(0., capture, 0., kp, ki, raw, [.5])


@pytest.mark.parametrize('key,scale', [('pid_ki', 1.1), ('pid_kp', .9), ('pid_integral_max', 2.)])
def test_baseline_differing_from_commissioned_profile_is_refused(tmp_path, key, scale):
    p = profile(); config = commissioned_config(p); config[key] *= scale
    with pytest.raises(DeviceError, match='differs from commissioned profile: ' + key):
        NativePIDTuning(Native(tmp_path, p, config), trial_parameters())


def test_uncommissioned_pi_is_refused(tmp_path):
    # Commissioning profile: integral_limit_count is a stage bound, the approved value is pending.
    p = profile(integral_limit_count=1000., approved_integral_limit_count=20000.)
    with pytest.raises(DeviceError, match='commissioning is in progress'):
        NativePIDTuning(Native(tmp_path, p), trial_parameters())
    native = Native(tmp_path, profile()); native.commissioning = True
    with pytest.raises(DeviceError, match='commissioning is in progress'):
        NativePIDTuning(native, trial_parameters())


def test_existing_refusals_are_kept(tmp_path):
    p = profile(); config = commissioned_config(p); config['pid_kd'] = 1.
    with pytest.raises(DeviceError, match='commissioned PI only'):
        NativePIDTuning(Native(tmp_path, p, config), trial_parameters())
    with pytest.raises(DeviceError, match='gain reduction exceeds device trial envelope'):
        NativePIDTuning(Native(tmp_path, profile()), trial_parameters((.7, .2)))
    # The transition step stays bounded by the commissioning stage bound (max_correction_count / 10).
    bad = profile(); bad['online_pid'] = dict(bad['online_pid'], max_step_count=121.)
    with pytest.raises(DeviceError, match='Invalid device PI transition bound'):
        NativePIDTuning(Native(tmp_path, bad), trial_parameters())


# ---- (2) capability report follows the owner-adopted profile ------------------------------------------------------

FF_FROZEN = dict(enabled=True, bounds=dict(current_per_temperature=[-.2, .2]), max_excursion=dict(current=.02, temperature=.01))


def rig_device(ff):
    return dict(type='ecdl.rig.owner', connection=dict(endpoint='http://127.0.0.1:1', instrument='sr422'),
                control_settings=dict(background_adjustments=dict(feedforward=copy.deepcopy(ff)),
                                      steady_lock=dict(tec_current_feedforward=dict(current_per_temperature=.5))))


@pytest.fixture
def frozen_gateway(tmp_path, monkeypatch):
    from test_agent_background_gateway import Runtime
    from experiment_core.records import atomic_json
    from types import SimpleNamespace
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read', lambda laser: dict(laser_id=laser['id'], mode='measurement', revision=0))
    runtime = Runtime(tmp_path / 'deployment')
    saved = dict(document=dict(id='laser', devices=dict(rig=rig_device(FF_FROZEN))))
    runtime.interface_repository = SimpleNamespace(read=lambda kind, identifier: saved)
    frozen = dict(id='laser', devices=dict(rig=rig_device(FF_FROZEN)))
    for identifier, operation, params in (
            ('task', 'agent.task', dict(instruction_profile='adjustment', monitor_job_id='monitor')),
            ('monitor', 'agent.monitor', dict(sample_interval_s=2)),
            ('controller', 'servo.ecdl_steady_lock', {})):
        runtime.jobs[identifier] = dict(id=identifier, set_id='laser-set', status='running', cards=[dict(operation=operation)])
        directory = runtime.directory(identifier); directory.mkdir(parents=True)
        atomic_json(directory / 'invocation.json', dict(parameters=params))
        (directory / 'laser.yaml').write_text(yaml.safe_dump(frozen) if identifier == 'controller' else 'id: laser\n')
    host = tmp_path / 'trusted'; (host / 'laser/controller').mkdir(parents=True); (host / 'laser/monitors').mkdir()
    atomic_json(host / 'laser/monitors/monitor.json', dict(generation='g', enabled=True, last_seen=100., max_age_s=30.,
        parameters_hash='h', bounds=dict(enabled=True, half_span_v=.01, max_points=8, timeout_s=100, handoff_timeout_s=10)))
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(host))
    monkeypatch.setattr('ldcon.agents.agent_background.time.time', lambda: 100.)
    return runtime, saved


def test_capability_report_follows_live_adopted_bound_change(frozen_gateway, monkeypatch):
    from ldcon.agents.agent_background import route
    from ldcon.core.adjustment_registry import capabilities
    from ldcon.core.interface_schema import digest
    runtime, saved = frozen_gateway
    adopted = rig_device(dict(FF_FROZEN, bounds=dict(current_per_temperature=[-.2, 2.])))
    calls = []
    def describe(laser):
        calls.append(laser['devices']['rig']['connection'])
        return dict(laser_id='laser', revision=digest(adopted),
                    background_capabilities=capabilities('servo.ecdl_steady_lock', adopted))
    monkeypatch.setattr('ldcon.agents.agent_background.owner_describe', describe)
    status = route(runtime, 'task')
    assert calls and status['capability_source'] == dict(source='owner_adopted', revision=digest(adopted))
    assert status['parameter_adjustments']['feedforward']['bounds'] == dict(current_per_temperature=[-.2, 2.])
    assert 'feedforward' in status['supported_adjustments']

    # An owner that predates background_capabilities in describe: the saved definition counts only when its
    # device digest IS the owner-adopted revision.
    monkeypatch.setattr('ldcon.agents.agent_background.owner_describe',
                        lambda laser: dict(laser_id='laser', revision=digest(adopted)))
    saved['document']['devices']['rig'] = copy.deepcopy(adopted)
    status = route(runtime, 'task')
    assert status['capability_source']['source'] == 'saved_equals_owner_adopted'
    assert status['parameter_adjustments']['feedforward']['bounds'] == dict(current_per_temperature=[-.2, 2.])
    saved['document']['devices']['rig'] = rig_device(dict(FF_FROZEN, bounds=dict(current_per_temperature=[-.2, 3.])))
    status = route(runtime, 'task')        # saved but not (yet) adopted: never shown, never the frozen copy either
    assert status['capability_source']['source'] == 'unavailable'
    assert 'feedforward' not in status['supported_adjustments'] and 'bounds' not in status['parameter_adjustments']['feedforward']

    def offline(laser):raise OSError('owner offline')
    monkeypatch.setattr('ldcon.agents.agent_background.owner_describe', offline)
    status = route(runtime, 'task')
    assert status['capability_source']['source'] == 'unavailable'
    assert status['supported_adjustments'] == ['current', 'pi']
    with pytest.raises(ValueError, match='does not support'):
        route(runtime, 'task', dict(action='begin', family='feedforward', request_id='ff', expected_operating_mode_revision=0,
                                    parameter_candidate=dict(current_per_temperature=.5)))


def test_rig_owner_describe_reports_the_adopted_envelope(tmp_path, monkeypatch):
    import hashlib
    from test_ecdl_settings_adopt import fake_rig, make_owner, save
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    laser = fake_rig.laser_definition()
    laser['devices']['rig']['control_settings']['background_adjustments'] = dict(feedforward=copy.deepcopy(FF_FROZEN))
    path = tmp_path / 'lasers' / 'sr422.yaml'; path.parent.mkdir(); path.write_text(yaml.safe_dump(laser))
    owner = make_owner(tmp_path, laser)
    try:
        owner.hw()
        report = owner.dispatch(dict(action='describe'))['background_capabilities']
        assert report['parameter_adjustments']['feedforward']['bounds'] == dict(current_per_temperature=[-.2, .2])
        candidate = copy.deepcopy(owner.laser)
        candidate['devices']['rig']['control_settings']['background_adjustments']['feedforward']['bounds'][
            'current_per_temperature'] = [-.2, 2.]
        revision = save(owner, candidate)
        assert owner.dispatch(dict(action='settings_adopt', saved_revision=revision))['status'] == 'adopted'
        described = owner.dispatch(dict(action='describe'))
        assert described['background_capabilities']['parameter_adjustments']['feedforward']['bounds'] == dict(
            current_per_temperature=[-.2, 2.])
        assert 'feedforward' in described['background_capabilities']['supported_adjustments']
        json.dumps(described)               # the lease-less reply stays JSON
    finally:
        owner.close(hold=False)


def test_sr422_background_trial_next_meets_the_unchanged_return_budget(tmp_path):
    """Documented remaining gap (2026-10-23 current-state note): with i20000 and the 10-count transition step the
    bounded return of a 0.7/0.5 reduction needs ~10^3 owner steps, far beyond the 20 s handoff budget. This change
    does not alter the step bound or the budget (both human decisions); the refusal is now the honest one."""
    from ldcon.owners.ecdl_background_pid import begin
    native = Native(tmp_path, profile())
    request = dict(parameters=dict(gain_factors=[.7, .5], save_settings=False), handoff_timeout_s=20.)
    with pytest.raises(ValueError, match=r'PI return needs .* s at the admitted cadence; handoff budget is 20 s'):
        begin(native, request, context={}, identity={}, interval=.1)
    assert not any('exceeds' in str(v) for _, v in native.o.records)
