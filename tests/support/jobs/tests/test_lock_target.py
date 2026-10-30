"""tune.lock_target (rule F, 2026-10-22): ONE target source in the laser definition, bounded change through the
dashboard configuration API, the live reload hook -> owner ramp, and the card's wait/verify."""
import json
import time
from pathlib import Path

import pytest

from ldcon import lock_target as lt
from ldcon.contracts import DeviceError
from ldcon.agent_authority import classify, live_class, FREE, SAFETY
from ldcon.interface_schema import validate_laser, load_mapping

TARGET = 710962838507871.5
LASER_YAML = """schema_version: 1
id: sr422
name: Sr422
allow_output: true
target:
  frequency_hz: 710962838507871.5
  bounds_hz: [710951147558089.4, 710973066296704.8]
  max_change_hz: 2000000000.0
devices:
  rig:
    type: ecdl.rig.owner
    connection: {}
    absolute_conditions: {}
interfaces:
  lock: {device: rig, capability: ecdl_stationary_lock}
"""


def laser():
    return load_mapping(LASER_YAML)


def test_single_source_resolution_and_validation():
    doc = laser()
    validate_laser(doc)                                                 # the schema accepts the target block
    assert lt.laser_target(doc)['frequency_hz'] == TARGET
    assert lt.resolve_target(doc, dict(target_frequency_hz=None)) == TARGET
    assert lt.resolve_target(doc, dict(target_frequency_hz=1.)) == 1.          # a pinned card parameter wins
    assert lt.resolve_target(doc, dict(target_frequency_hz=None, target_wavelength_nm=421.)) == pytest.approx(lt.C_NM / 421.)
    assert lt.resolve_target(dict(id='x'), dict(target_frequency_hz=None)) is None
    bad = laser(); bad['target']['frequency_hz'] = 1.
    with pytest.raises(ValueError, match='outside'):
        validate_laser(bad)
    bad = laser(); bad['target']['extra'] = 1
    with pytest.raises(ValueError):
        validate_laser(bad)
    # authority: the value is free (the card may save it); its bounds are safety-class; the live class routes it
    assert classify('lasers', ('target', 'frequency_hz')) == FREE and classify('lasers', ('target', 'bounds_hz', 0)) == SAFETY
    assert live_class('lasers', ('target', 'frequency_hz')) == 'target_hot' and live_class('lasers', ('target', 'max_change_hz')) == 'snapshot'


def test_change_checks_and_verbatim_text_edit():
    doc = laser()
    assert lt.check_change(doc, TARGET + 100e6) == TARGET
    with pytest.raises(DeviceError, match='outside target.bounds_hz'):
        lt.check_change(doc, 710980e9)
    with pytest.raises(DeviceError, match='exceeds the cap'):
        lt.check_change(doc, TARGET + 3e9)
    assert lt.check_change(doc, TARGET + 3e9, max_change_hz=5e9) == TARGET
    no_bounds = laser(); del no_bounds['target']['bounds_hz']
    with pytest.raises(DeviceError, match='bounds_hz'):
        lt.check_change(no_bounds, TARGET)
    edited = lt.edit_target_text(LASER_YAML, TARGET + 100e6)
    assert load_mapping(edited)['target']['frequency_hz'] == TARGET + 100e6
    assert edited.replace(repr(TARGET + 100e6), '710962838507871.5') == LASER_YAML     # nothing else changed
    with pytest.raises(DeviceError, match='no top-level target'):
        lt.edit_target_text(LASER_YAML.replace('target:', 'other:'), 1.)
    for bad in (dict(target_frequency_hz=1., offset_hz=2.), dict(verify_samples=-1), dict(offset_hz=0.), dict(bogus=1)):
        with pytest.raises(ValueError):
            lt.validate(bad)


class FakeApi:
    def __init__(self, text):
        self.text, self.revision, self.puts = text, 'r0', []

    def get_configuration(self, kind, name):
        return dict(document=load_mapping(self.text), yaml=self.text, revision=self.revision)

    def put_configuration(self, kind, name, text, expected_revision, **kw):
        assert expected_revision == self.revision
        self.text, self.revision = text, 'r1'
        self.puts.append((kind, name, kw))
        return dict(revision='r1')


def job_tree(tmp_path, locked, pinned=False, supported=True):
    lock_dir = tmp_path / 'jobs' / 'sr422' / 'lockjob'
    card_dir = tmp_path / 'jobs' / 'sr422' / 'cardjob'
    lock_dir.mkdir(parents=True); card_dir.mkdir()
    if locked:
        (lock_dir / 'job.json').write_text(json.dumps(dict(id='lockjob', laser=dict(id='sr422'),
            cards=[dict(operation='servo.ecdl_steady_lock', params=dict(target_frequency_hz=TARGET if pinned else None))])))
        (lock_dir / 'progress.json').write_text(json.dumps(dict(status='running', controller_state='locked')))
        (lock_dir / 'laser-live.json').write_text(json.dumps(dict(supported=supported)))
    return lock_dir, card_dir


def run_card(card_dir, api, p, now, laser_doc=None):
    return lt.run_lock_target(card_dir, laser_doc or laser(), dict(id='lock-target'), p, clock=lambda: now[0],
                              sleep=lambda s: now.__setitem__(0, now[0] + s), cancel=lambda: False, emit=lambda k, v: None, api=api)


def test_unlocked_change_saves_the_single_source_only(tmp_path):
    lock_dir, card_dir = job_tree(tmp_path, locked=False)
    api = FakeApi(LASER_YAML)
    result = run_card(card_dir, api, dict(offset_hz=150e6), [0.])
    assert result['status'] == 'succeeded' and not result['locked'] and result['target_frequency_hz'] == TARGET + 150e6
    assert load_mapping(api.text)['target']['frequency_hz'] == TARGET + 150e6 and api.puts[0][2]['actor'] == 'card:lock-target'
    with pytest.raises(DeviceError, match='exceeds the cap'):
        run_card(card_dir, api, dict(target_frequency_hz=TARGET + 5e9), [0.])
    assert len(api.puts) == 1


def test_locked_change_waits_for_the_worker_reload_then_the_ramp_and_verifies(tmp_path):
    lock_dir, card_dir = job_tree(tmp_path, locked=True)
    api = FakeApi(LASER_YAML)
    now = [0.]
    import threading
    def worker():
        # the lock worker: adopts the reload (target_hot applied), ramps, then holds at the new target
        for _ in range(100):
            if api.revision == 'r1':
                break
            time.sleep(.01)
        (lock_dir / 'laser-reload-result-abc.json').write_text(json.dumps(dict(request_id='abc', status='applied', revision_before='r0', revision_after='r1', updated_unix_s=time.time(), applied=['target.frequency_hz'])))
        for k in range(3):
            (lock_dir / 'lock-target.json').write_text(json.dumps(dict(target_frequency_hz=TARGET + 100e6, ramped_target_hz=TARGET + 25e6 * (k + 1), ramp_active=True,
                                                                        frequency_hz=TARGET + 25e6 * (k + 1), single_mode=True, updated_unix_s=time.time())))
            time.sleep(.02)
        for k in range(20):
            (lock_dir / 'lock-target.json').write_text(json.dumps(dict(target_frequency_hz=TARGET + 100e6, ramped_target_hz=TARGET + 100e6, ramp_active=False,
                                                                        frequency_hz=TARGET + 100e6 + 1e5 * k, single_mode=True, updated_unix_s=time.time())))
            time.sleep(.05)
    t = threading.Thread(target=worker); t.start()
    published = []
    result = lt.run_lock_target(card_dir, laser(), dict(id='lock-target'), dict(offset_hz=100e6, tolerance_hz=2e6, verify_samples=3, sample_interval_s=.1),
                                clock=time.monotonic, sleep=time.sleep, cancel=lambda: False, emit=lambda k, v: None, api=api, publish=published.append)
    t.join(5)
    assert result['status'] == 'succeeded' and result['locked'] and result['verify']['ok'] and result['verify']['samples'] == 3
    assert any(p['status'] == 'ramping' for p in published)


def test_locked_change_refuses_a_pinned_lock_target_or_an_old_worker(tmp_path):
    lock_dir, card_dir = job_tree(tmp_path, locked=True, pinned=True)
    with pytest.raises(DeviceError, match='pins its own target'):
        run_card(card_dir, FakeApi(LASER_YAML), dict(offset_hz=1e6), [0.])
    lock_dir, card_dir = job_tree(tmp_path / 'b', locked=True, supported=False)
    with pytest.raises(DeviceError, match='predates live laser reload'):
        run_card(card_dir, FakeApi(LASER_YAML), dict(offset_hz=1e6), [0.])


def test_laser_live_routes_the_target_to_the_lock(tmp_path):
    """The laser reload hook: a changed target.frequency_hz reaches lock.set_target (owner ramp) when the card
    follows the laser target, and stays a snapshot when the card pins its own target."""
    from ldcon.laser_live import LaserLive, enqueue_reload, laser_file
    from ldcon.agent_authority import describe_changes
    job = tmp_path / 'jobs' / 'sr422' / 'lockjob'
    job.mkdir(parents=True)
    (tmp_path / 'lasers').mkdir()
    before = laser()
    text_before = LASER_YAML
    (job / 'job.json').write_text(json.dumps(dict(id='lockjob', laser_revision=lt.__name__ and __import__('hashlib').sha256(text_before.encode()).hexdigest())))
    class Lock:
        uncertain_output = False
        def __init__(self): self.targets = []
        def set_target(self, hz): self.targets.append(hz); return dict(target_frequency_hz=hz)
    lock = Lock()
    live = LaserLive(job, json.loads(json.dumps(before)), dict(id='steady-lock', operation='servo.ecdl_steady_lock', parameters={}), lambda k, v: None, lock=lock)
    live.follow_laser_target = True
    text_after = lt.edit_target_text(text_before, TARGET + 50e6)
    laser_file(job, 'sr422').write_text(text_after)
    after = load_mapping(text_after)
    rows = describe_changes('lasers', before, after)
    for row in rows:
        row['live'] = live_class('lasers', tuple(row['path'].split('.')))
    from ldcon.laser_live import text_revision
    request = enqueue_reload(job, laser_id='sr422', revision_before=text_revision(text_before), revision_after=text_revision(text_after), rows=rows, decision={})
    assert request['status'] == 'queued'
    result = live.poll('steady_cycle')
    assert result['status'] == 'applied' and 'target.frequency_hz' in result['applied'] and lock.targets == [TARGET + 50e6]
    assert live.laser['target']['frequency_hz'] == TARGET + 50e6
    # pinned card: snapshot, no owner call
    lock2 = Lock()
    live2 = LaserLive(job, json.loads(json.dumps(before)), dict(id='steady-lock', operation='servo.ecdl_steady_lock', parameters={}), lambda k, v: None, lock=lock2)
    live2.revision = text_revision(text_before)
    live2.follow_laser_target = False
    enqueue_reload(job, laser_id='sr422', revision_before=text_revision(text_before), revision_after=text_revision(text_after), rows=rows, decision={})
    result = live2.poll('steady_cycle')
    assert result['status'] == 'snapshot' and 'target.frequency_hz' in result['snapshot'] and lock2.targets == []


def _optical_laser(warning=0.35):
    doc = laser()
    doc['devices']['rig']['control_settings'] = dict(optical=dict(warning_max=dict(side_peak_height_max=warning)),
                                                    entry_gate=dict(mode_id='desired'))
    return doc


def test_quality_reporting_uses_mode_and_clear_degradation_not_the_strict_single_mode(tmp_path):
    """Quality remains observable even though it no longer gates frequency-arrival completion."""
    doc = _optical_laser()
    ok = lambda state: lt.sample_mode_ok(state, doc, tmp_path)
    assert ok(dict(single_mode=False, mode_id='desired', degraded=False))
    assert ok(dict(single_mode=False, degraded=False))                       # mode_id not reported
    assert not ok(dict(single_mode=True, mode_id='other', degraded=False))
    assert not ok(dict(single_mode=True, mode_id='desired', degraded=True))
    # a lock whose worker predates the 'degraded' field: judged from its fresh live trace vs optical.warning_max
    (tmp_path / 'live-trace.json').write_text(json.dumps([dict(acquired_s=time.time(), fringe_metrics=dict(side_peak_height_max=.30))]))
    assert ok(dict(single_mode=False))
    (tmp_path / 'live-trace.json').write_text(json.dumps([dict(acquired_s=time.time(), fringe_metrics=dict(side_peak_height_max=.40))]))
    assert not ok(dict(single_mode=False))


def test_a_borderline_strict_single_mode_lock_verifies(tmp_path):
    lock_dir, card_dir = job_tree(tmp_path, locked=True)
    api = FakeApi(LASER_YAML)
    import threading

    def worker():
        for _ in range(100):
            if api.revision == 'r1':
                break
            time.sleep(.01)
        (lock_dir / 'laser-reload-result-abc.json').write_text(json.dumps(dict(request_id='abc', status='applied', revision_before='r0', revision_after='r1', updated_unix_s=time.time(), applied=['target.frequency_hz'])))
        for k in range(40):
            (lock_dir / 'lock-target.json').write_text(json.dumps(dict(target_frequency_hz=TARGET + 100e6, ramped_target_hz=TARGET + 100e6, ramp_active=False, frequency_hz=TARGET + 100e6 + 4e5,
                                                                        single_mode=False, mode_id='desired', degraded=False, updated_unix_s=time.time())))
            time.sleep(.05)
    t = threading.Thread(target=worker); t.start()
    result = lt.run_lock_target(card_dir, _optical_laser(), dict(id='lock-target'), dict(offset_hz=100e6, tolerance_hz=2e6, verify_samples=3, sample_interval_s=.1, timeout_s=10.),
                                clock=time.monotonic, sleep=time.sleep, cancel=lambda: False, emit=lambda k, v: None, api=api)
    t.join(5)
    assert result['status'] == 'succeeded' and result['verify']['ok']


def test_the_sr422_producer_reports_clear_degradation_not_the_strict_flag():
    from ldcon.ecdl_steady import clearly_degraded
    doc = _optical_laser()
    doc['interfaces']['lock'] = dict(device='rig', capability='ecdl_stationary_lock')
    assert clearly_degraded(dict(single_mode=False, metrics=dict(side_peak_height_max=.30)), doc) is False
    assert clearly_degraded(dict(single_mode=True, metrics=dict(side_peak_height_max=.40)), doc) is True
    assert clearly_degraded(dict(maintenance_required=True, metrics={}), doc) is True
