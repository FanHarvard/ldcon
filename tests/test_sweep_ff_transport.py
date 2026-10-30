"""Registered sweep orchestration without hardware or database access."""
import copy
import json
import time

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.progress import CancelledRun
from ldcon.jobs.sweep_ff_transport import SweepExperiment, normalize_outcome, raw_analysis


class Clock:
    def __init__(self):
        self.value = 0.
        self.after_sleep = lambda: None

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        self.after_sleep()


@pytest.fixture
def rig(tmp_path):
    directory = tmp_path/'jobs'/('a'*32)
    directory.mkdir(parents=True)
    (directory/'job.json').write_text(json.dumps(dict(set_id='laser',set_revision='set-r1')))
    laser = dict(id='laser', target={'frequency_hz':300e12},
                 devices={'rig':dict(type='ecdl.rig.owner', control_settings={'locked_sweep':{}})},
                 interfaces={'servo_modulation':{'device':'rig'}})
    card = dict(id='staircase', operation='sweep.servo_modulation', uses=['servo_modulation'])
    deck = dict(id='deck', cards=[card])
    clock = Clock()
    flags = dict(cancel=False)
    lock = dict(id='b'*32, laser={'id':'laser'}, set_id='laser', deck_id='deck', status='running',
                cards=[dict(id='lock', operation='servo.ecdl_steady_lock')])
    lock_directory = directory.parent/lock['id']
    lock_directory.mkdir()
    (lock_directory/'lock-target.json').write_text(json.dumps(dict(
        target_frequency_hz=laser['target']['frequency_hz'], updated_unix_s=time.time(), ramp_active=False)))

    class API:
        def __init__(self):
            self.calls, self.children, self.jobs, self.stops = [], [], {}, []
            self.revision, self.active = 'r1', [lock]
            self.set_revision = 'set-r1'
            self.running_reads, self.analysis_pending = 1, 0
            self.fail_start = self.fail_stop = self.cleanup_never_finishes = False
            self.start_id = self.analysis_result = None
            self.projected_cards = False
            self.child_outcome = dict(restored=True, playback_completed=True, verdict='succeeded')

        def get_configuration(self, *_):
            return dict(revision=self.revision, document=copy.deepcopy(laser))

        def _request(self, method, path, body=None):
            self.calls.append((method, path, copy.deepcopy(body)))
            if path == 'api/jobs?active_only=true': return dict(jobs=copy.deepcopy(self.active))
            if path == 'api/sets/laser': return dict(revision=self.set_revision)
            if path.endswith('/start'):
                journal = json.loads((directory/'sweep-ff-transport.json').read_text())
                assert journal['children'][-1]['state'] == 'dispatching'
                assert journal['children'][-1]['request'] == body
                if self.fail_start: raise OSError('response lost')
                child = self.start_id or ('c' if not self.children else 'd')*32
                self.children.append(child)
                self.jobs[child] = dict(id=child, laser={'id':'laser'}, set_id='laser', deck_id='deck',
                    cards=[{'id':'staircase', 'card' if self.projected_cards else 'operation':card['operation']}],
                    status='running')
                return dict(id=child)
            child = path.split('/')[2]
            if path.endswith('/stop'):
                assert child != lock['id'] and child != directory.name
                self.stops.append(child)
                if self.fail_stop: raise OSError('stop response lost')
                return dict(status='stopping')
            if path.endswith('/analysis'):
                if self.analysis_pending:
                    self.analysis_pending -= 1
                    return dict(state='pending')
                doc = self.analysis_result or dict(schema='ldcon.shot-analysis/v1', proxy_job=child,
                    source='influx', mode='levels', points=dict(t_unix_s=[1.,1.01,1.02], wavelength_nm=[1000.]*3),
                    parameters={'reference':dict(lock_target_hz=laser['target']['frequency_hz'],
                        f_ref_hz=laser['target']['frequency_hz'],f_ref_minus_target_hz=0.)},
                    quality=dict(clock_synced=True, clock_step_in_window_us=0.), completeness={'complete':True})
                return dict(state='ready', result=copy.deepcopy(doc))
            job = copy.deepcopy(self.jobs[child])
            if child in self.stops:
                if self.cleanup_never_finishes: return job
                return dict(job, status='cancelled', outcome=dict(restored=True, playback_completed=False, verdict='cancelled'))
            if self.running_reads:
                self.running_reads -= 1
                return job
            return dict(job, status='succeeded', outcome=copy.deepcopy(self.child_outcome))

    api = API()
    def make(**kwargs):
        args = dict(timeout_s=30., poll_s=.1, cleanup_timeout_s=.3, analysis_wait_s=.5,
                    clock=clock, sleep=clock.sleep, cancel=lambda:flags['cancel'], emit=lambda *_:None,
                    publish=lambda *_:None, api=api)
        args.update(kwargs)
        return SweepExperiment(directory, laser, deck, 'staircase', **args)
    params = dict(shape='staircase', trigger='auto', levels_hz=[-1e6,0,1e6], dwell_s=.1,
                  full_data_log_ms=0, calibrate_only=False, shot_sweep=None)
    return dict(directory=directory, laser=laser, deck=deck, card=card, clock=clock,
                flags=flags, api=api, lock=lock, make=make, params=params)


def test_normal_children_are_serial_and_restored(rig):
    experiment = rig['make']()
    first, second = experiment.run(rig['params']), experiment.run(rig['params'])
    assert first['child_id'] != second['child_id']
    assert first['analysis']['points']['wavelength_nm'] == [1000.]*3
    assert first['owner_outcome']['restored'] and first['owner_outcome']['playback_completed']
    assert first['owner_outcome']['mode_hop'] is None
    assert not first['owner_outcome']['outputs_uncertain'] and not rig['api'].stops
    journal = json.loads((rig['directory']/'sweep-ff-transport.json').read_text())
    assert len(journal['children']) == 2 and all(c['state'] == 'finished' for c in journal['children'])
    assert not any('target' in path for _, path, _ in rig['api'].calls)


def test_native_route_uses_registered_current_sweep(rig):
    laser = rig['laser']
    laser['devices'] = dict(awg={'control_settings':{'native_feedforward_awg':{}}},
                            native={'control_settings':{'native_ff_sweep':{}}})
    laser['interfaces'] = dict(current_modulation={'device':'awg'}, native_current={'device':'native'})
    rig['card'].update(operation='sweep.current_modulation', uses=['current_modulation'])
    rig['lock']['cards'][0]['operation'] = 'servo.native_current'
    assert rig['make']().run(rig['params'])['context']['lock_operation'] == 'servo.native_current'


def test_normal_proxy_fact_outcome_and_projected_card_metadata(rig):
    from ldcon.jobs.sweep_verdict import Facts
    facts = Facts(1., [-1e6,0.,1e6])
    facts.observe(dict(state='playing', playback_started_unix_s=1.))
    facts.observe(dict(state='done'))
    outcome = facts.outcome(dict(state='restored', hardware_restored=True,
                                result_status='completed_unsettled'))
    rig['api'].child_outcome = outcome
    rig['api'].projected_cards = True
    result = rig['make']().run(rig['params'])
    assert result['owner_outcome']['restored'] is True
    assert result['owner_outcome']['playback_completed'] is True
    assert result['owner_outcome']['censored'] is False
    assert result['owner_outcome']['original'] == outcome


def test_unknown_start_is_not_retried_or_stopped(rig):
    rig['api'].fail_start = True
    experiment = rig['make']()
    result = experiment.run(rig['params'])
    assert result['owner_outcome']['outputs_uncertain'] and result['child_id'] is None
    assert 'may have been accepted' in result['owner_outcome']['reason']
    with pytest.raises(DeviceError, match='closed'): experiment.run(rig['params'])
    assert len([p for m,p,_ in rig['api'].calls if p.endswith('/start')]) == 1
    assert not rig['api'].stops


@pytest.mark.parametrize('cause', ['cancel', 'target', 'foreground', 'lock', 'progress'])
def test_interruption_stops_only_own_child_then_verifies_restoration(rig, cause):
    api, clock = rig['api'], rig['clock']
    api.running_reads = 10
    def change():
        if cause == 'cancel': rig['flags']['cancel'] = True
        if cause == 'target': api.revision = 'new-target'
        if cause == 'foreground':
            api.active.append(dict(id='e'*32, laser={'id':'laser'}, status='running', cards=[{'operation':'tune.lock_target'}]))
        if cause == 'lock': api.active.clear()
    clock.after_sleep = change
    def publish(_):
        if cause == 'progress': raise CancelledRun()
    result = rig['make'](publish=publish).run(rig['params'])
    assert result['owner_outcome']['cancelled'] and result['owner_outcome']['restored']
    assert result['owner_outcome']['censored'] and api.stops == ['c'*32]
    assert result['owner_outcome']['cleanup']['record_may_finish_before_restore'] is True


def test_unknown_stop_not_replayed_cleanup_bounded(rig):
    api, clock = rig['api'], rig['clock']
    api.running_reads, api.fail_stop, api.cleanup_never_finishes = 100, True, True
    clock.after_sleep = lambda:rig['flags'].update(cancel=True)
    result = rig['make']().run(rig['params'])
    assert result['owner_outcome']['outputs_uncertain']
    assert result['owner_outcome']['cleanup']['state'] == 'timeout'
    assert api.stops == ['c'*32] and clock() < 1.


def test_unverified_restore_prevents_next_trial(rig):
    rig['api'].child_outcome['restored'] = False
    experiment = rig['make']()
    result = experiment.run(rig['params'])
    assert result['analysis'] is None and result['owner_outcome']['outputs_uncertain']
    with pytest.raises(DeviceError, match='closed'): experiment.run(rig['params'])


def test_failed_owner_outcome_is_censored_even_if_record_played_and_restored(rig):
    rig['api'].child_outcome['verdict'] = 'failed'
    experiment = rig['make']()
    result = experiment.run(rig['params'])
    assert result['analysis'] is not None and result['owner_outcome']['restored']
    assert result['owner_outcome']['playback_completed'] and result['owner_outcome']['censored']
    with pytest.raises(DeviceError, match='closed'):experiment.run(rig['params'])


def test_sparse_summary_does_not_replace_raw_data(rig):
    rig['api'].analysis_result = dict(schema='ldcon.shot-analysis/v1', proxy_job='c'*32,
        source='influx', mode='levels', summary={'rms_error_hz':1.},
        parameters={'reference':{'lock_target_hz':rig['laser']['target']['frequency_hz']}})
    result = rig['make']().run(rig['params'])
    assert result['analysis'] is None and result['owner_outcome']['restored']
    assert result['owner_outcome']['censored'] and 'arrays' in result['owner_outcome']['reason']


def test_analysis_wait_is_bounded_and_does_not_relaunch(rig):
    rig['api'].analysis_pending = 100
    result = rig['make']().run(rig['params'])
    assert result['analysis'] is None and result['owner_outcome']['restored']
    assert 'deadline' in result['owner_outcome']['reason'] and rig['clock']() < 1.
    writes = [p for m,p,_ in rig['api'].calls if m == 'POST']
    assert len(writes) == 1 and writes[0].endswith('/start')


def test_gateway_monitor_allowed_but_physical_agent_task_preempts(rig):
    rig['api'].active.extend(dict(id=str(i)*32, laser={'id':'laser'}, status='running', cards=[{'operation':op}])
                             for i,op in enumerate(('agent.resident','agent.monitor'), 5))
    experiment = rig['make']()
    experiment.check()
    rig['api'].active.append(dict(id='8'*32, laser={'id':'laser'}, status='running', cards=[{'operation':'agent.task'}]))
    with pytest.raises(CardCancelled, match='Foreground'): experiment.check()


def test_pending_controller_command_blocks_before_dispatch(rig):
    folder = rig['directory'].parent/rig['lock']['id']
    folder.mkdir(exist_ok=True)
    experiment = rig['make']()
    (folder/'hot-control-request.json').write_text('{}')
    with pytest.raises(CardCancelled, match='pending'): experiment.run(rig['params'])
    assert not rig['api'].children


@pytest.mark.parametrize('name,value', [
    ('runtime-parameter-inflight-abcdef.json', {}),
    ('online-status.json', {'status':'running'}),
    ('hot-control-status.json', {'status':'dispatching'}),
])
def test_consumed_request_still_blocks_while_operation_inflight(rig, name, value):
    folder = rig['directory'].parent/rig['lock']['id']
    folder.mkdir(exist_ok=True)
    experiment = rig['make']()
    (folder/name).write_text(json.dumps(value))
    with pytest.raises(CardCancelled):experiment.run(rig['params'])
    assert not rig['api'].children


def test_assignment_change_never_starts_changed_child_definition(rig):
    experiment = rig['make']()
    rig['api'].set_revision = 'new-deck'
    with pytest.raises(CardCancelled, match='Assigned deck'): experiment.run(rig['params'])
    assert not rig['api'].children


def test_assignment_change_during_trial_stops_only_known_child(rig):
    rig['api'].running_reads = 100
    rig['clock'].after_sleep = lambda:setattr(rig['api'], 'set_revision', 'new-deck')
    result = rig['make']().run(rig['params'])
    assert result['owner_outcome']['cancelled'] and result['owner_outcome']['restored']
    assert rig['api'].stops == ['c'*32]


def test_existing_parent_assignment_revision_must_be_available(rig):
    (rig['directory']/'job.json').write_text(json.dumps(dict(set_id='laser')))
    with pytest.raises(DeviceError, match='Frozen assignment'):rig['make']()
    assert not rig['api'].calls


def test_unrelated_start_response_never_authorizes_stopping_parent(rig):
    rig['api'].start_id = rig['lock']['id']
    result = rig['make']().run(rig['params'])
    assert result['child_id'] is None and result['owner_outcome']['outputs_uncertain']
    assert not rig['api'].stops


def test_existing_journal_never_replayed(rig):
    rig['make']()
    with pytest.raises(DeviceError, match='no replay'): rig['make']()


def test_wait_checks_cancel_without_output(rig):
    experiment = rig['make']()
    experiment.wait(.2)
    rig['flags']['cancel'] = True
    with pytest.raises(CardCancelled): experiment.wait(.2)
    assert not any(m == 'POST' for m,_,_ in rig['api'].calls)


@pytest.mark.parametrize('bad', [dict(shape='triangle'), dict(trigger='continuous'), dict(calibrate_only=True)])
def test_rejects_other_algorithm_before_dispatch(rig, bad):
    with pytest.raises(ValueError): rig['make']().run({**rig['params'], **bad})
    assert not rig['api'].children


def test_optical_degradation_is_not_a_mode_hop():
    job = dict(status='succeeded', outcome=dict(restored=True, playback_completed=True, tracking={'reason':'mode_degraded'}))
    assert normalize_outcome(job)['mode_hop'] is None
    job['outcome']['mode_hop'] = True
    assert normalize_outcome(job)['mode_hop'] is True


def test_raw_analysis_rejects_wrong_child():
    payload = dict(state='ready', result=dict(schema='ldcon.shot-analysis/v1', proxy_job='other', source='influx'))
    with pytest.raises(DeviceError, match='wrong child'): raw_analysis(payload, 'expected')


def test_missing_history_configuration_rejects_before_dashboard_or_child(rig):
    with pytest.raises(DeviceError, match='post-shot analysis'):
        rig['make'](api=None)
    assert not rig['api'].calls
    assert not (rig['directory']/'sweep-ff-transport.json').exists()


@pytest.mark.parametrize('setting', ['off','0','false'])
def test_disabled_history_rejects_before_dashboard_or_child(rig, monkeypatch, setting):
    monkeypatch.setenv('LDCON_SHOT_ANALYSIS', setting)
    with pytest.raises(DeviceError, match='analysis is disabled'):rig['make'](api=None)
    assert not rig['api'].calls


@pytest.mark.parametrize('field,value', [
    ('target_frequency_hz',None), ('target_frequency_hz',True),
    ('updated_unix_s',-60.), ('updated_unix_s',60.),
])
def test_initial_lock_target_requires_finite_fresh_evidence(rig, field, value):
    # Collection can precede this test by more than a minute in the full suite.
    if field=='updated_unix_s':value=time.time()+value
    path=rig['directory'].parent/rig['lock']['id']/'lock-target.json'
    data=json.loads(path.read_text());data[field]=value;path.write_text(json.dumps(data))
    with pytest.raises(DeviceError, match='fresh finite lock target'):rig['make']()
    assert not rig['api'].children


def test_missing_initial_lock_target_is_not_inferred_from_saved_target(rig):
    (rig['directory'].parent/rig['lock']['id']/'lock-target.json').unlink()
    with pytest.raises(DeviceError, match='fresh finite lock target'):rig['make']()
    assert not rig['api'].children


def test_runtime_only_target_change_preempts_even_without_configuration_revision_change(rig):
    experiment=rig['make']()
    path=rig['directory'].parent/rig['lock']['id']/'lock-target.json'
    data=json.loads(path.read_text());data['target_frequency_hz']+=20e6;path.write_text(json.dumps(data))
    with pytest.raises(CardCancelled, match='Lock target changed'):experiment.run(rig['params'])
    assert not rig['api'].children


def test_stale_lock_report_during_own_finite_record_is_not_an_owner_failure(rig):
    path=rig['directory'].parent/rig['lock']['id']/'lock-target.json'
    def stale():
        data=json.loads(path.read_text());data['updated_unix_s']=time.time()-60.;path.write_text(json.dumps(data))
    experiment=rig['make']()
    rig['clock'].after_sleep=stale
    result=experiment.run(rig['params'])
    assert result['owner_outcome']['restored'] and not result['owner_outcome']['censored']
    with pytest.raises(CardCancelled, match='report is stale'):experiment.run(rig['params'])
    assert len(rig['api'].children)==1 and not rig['api'].stops


def test_wrong_raw_reference_cannot_qualify_same_child_waveform(rig):
    doc=rig['api']._request('GET','api/jobs/'+'c'*32+'/analysis')['result']
    doc['parameters']['reference']['lock_target_hz']+=20e6
    rig['api'].analysis_result=doc
    result=rig['make']().run(rig['params'])
    assert result['owner_outcome']['restored'] and result['owner_outcome']['censored']
    assert result['analysis'] is None and 'frozen lock target' in result['owner_outcome']['reason']
