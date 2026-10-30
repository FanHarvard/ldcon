"""Target response studies: sampled evidence, bounded writes and foreground priority."""
import copy
import json
import math
from pathlib import Path

import pytest
import yaml

from ldcon.jobs import target_response as study
from ldcon.jobs.target_response_analysis import analyse, C_NM
from ldcon.drivers.influx_wavelength import InfluxWavelengthHistory


def config():
    return study.parameters({'parameters':dict(offsets_hz=[20e6,0.], baseline_s=.1, observation_s=1., evaluation_window_s=.2,
        check_interval_s=.05, confirmation_s=.4, max_gap_s=.09, minimum_samples=3,
        expected_settle_s=.3, settling_goal_s=.3, data_wait_s=.5)}, {})


def history(start, end, target=300e12, error=lambda t: 0.):
    n = int(round((end-start)/.05))
    return dict(samples=[dict(time_unix_s=start+i*.05, wavelength_nm=C_NM/(target+error(start+i*.05)),
                             system_seq=i, received_unix_s=start+i*.05)
                         for i in range(n+1)], integrity_errors=[])


def test_settling_rejects_lucky_crossing_and_reports_mean_rms_std_separately():
    p=config()
    data=history(0, 3, error=lambda t: 25e6 if t < .4 or .8 < t < 1.2 else 2e6)
    r=analyse(data,300e12,0,.3,3,p)
    assert r['status']=='settled'
    assert 1.19 <= r['settling_from_request_s'] <= 1.26
    w=r['final_window']
    assert w['mean_error_hz']==pytest.approx(2e6,abs=.1)
    assert w['rms_error_hz']==pytest.approx(2e6,abs=.1)
    assert w['stddev_hz'] < .1
    assert r['settling_after_apply_interval_s'][1] == r['settling_from_request_s']


@pytest.mark.parametrize('damage',['gap','tail','integrity','too_few'])
def test_missing_or_conflicting_data_are_inconclusive_not_settled(damage):
    d=history(0,2)
    if damage=='gap':d['samples']=[r for r in d['samples'] if not .5 < r['time_unix_s'] < 1]
    if damage=='tail':d['samples']=d['samples'][:-8]
    if damage=='integrity':d['integrity_errors']=['sequence_regression']
    if damage=='too_few':d['samples']=d['samples'][:2]
    assert analyse(d,300e12,0,.2,2,config())['status']=='inconclusive'


def test_rms_and_optional_peak_criteria():
    d=history(0,2,error=lambda t: 12e6*(-1 if round(t/.05)%2 else 1))
    p=config();p['mean_error_max_hz']=20e6
    assert analyse(d,300e12,0,.2,2,p)['status']=='not_settled'
    p['rms_error_max_hz']=20e6;p['peak_error_max_hz']=10e6
    assert analyse(d,300e12,0,.2,2,p)['status']=='not_settled'


def test_influx_reads_all_new_sequences_and_retains_timestamp_provenance(monkeypatch):
    from ldcon.jobs import shot_analysis
    fields={'system_value':{'2026-10-24T00:00:00.000Z':1000.,'2026-10-24T00:00:00.050Z':1000.,
                            '2026-10-24T00:00:00.100Z':1001.},
        'system_seq':{'2026-10-24T00:00:00.000Z':1,'2026-10-24T00:00:00.050Z':1,'2026-10-24T00:00:00.100Z':2},
        'is_new_system_value':{f'2026-10-24T00:00:00.{i}Z':1 for i in ('000','050','100')},
        'system_latency_us':{'2026-10-24T00:00:00.000Z':1000}}
    monkeypatch.setattr(shot_analysis,'_influx_series',lambda _:fields)
    calls=[]
    reader=InfluxWavelengthHistory(dict(esp32_host='192.0.2.1',influx=dict(bucket='test',measurement='native')),
        query=lambda cfg,q,**kw:calls.append(q) or '')
    r=reader.read(1792800000,1792800001)
    assert [x['system_seq'] for x in r['samples']]==[1,2]
    assert r['samples'][0]['latency_corrected'] is True
    assert r['samples'][1]['latency_corrected'] is False
    assert r['provenance']['aggregation']=='none'
    assert 'aggregateWindow' not in calls[0] and 'system_seq' in calls[0]


class Clock:
    def __init__(self):self.t=1000.;self.callback=lambda:None
    def __call__(self):return self.t
    def sleep(self,s):self.t+=s;self.callback()


class API:
    def __init__(self,clock):
        self.clock=clock;self.revision=0;self.writes=[];self.extra=[]
        self.doc=dict(id='test',allow_output=True,target=dict(frequency_hz=300e12,bounds_hz=[299e12,301e12],max_change_hz=1e9))
    def _request(self,*_):
        return {'jobs':[dict(id='parent',status='running',set_id='test',cards=[{'operation':'servo.native_current'}]),*self.extra]}
    def get_configuration(self,*_):return dict(revision=str(self.revision),document=copy.deepcopy(self.doc),yaml=yaml.safe_dump(self.doc))
    def put_configuration(self,kind,name,text,expected_revision,**kwargs):
        assert expected_revision==str(self.revision)
        self.doc=yaml.safe_load(text);self.revision+=1
        self.writes.append((self.clock(),self.doc['target']['frequency_hz']))
        return dict(revision=str(self.revision))


@pytest.fixture
def rig(tmp_path,monkeypatch):
    from ldcon.jobs import operating_point
    directory=tmp_path/'jobs/test/study';directory.mkdir(parents=True)
    clock=Clock();api=API(clock)
    monkeypatch.setattr(operating_point,'find_running_lock',lambda *_:(directory.parent/'parent',{'id':'parent'},{}))
    def runner(directory,laser,card,p,**kw):
        gateway=kw['api'];old=gateway.get_configuration('lasers','test')
        doc=old['document'];doc['target']['frequency_hz']=p['target_frequency_hz']
        reply=gateway.put_configuration('lasers','test',yaml.safe_dump(doc),old['revision'])
        clock.sleep(.2);kw['cancel']()
        return dict(locked=True,status='succeeded',revision=reply['revision'])
    class History:
        def read(self,start,end):
            return history(start,end,target=api.doc['target']['frequency_hz'])
    kwargs=dict(clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *a:None,
                api=api,history=History(),wall_clock=clock,target_runner=runner)
    return directory,clock,api,kwargs


def run(rig,**extra):
    directory,clock,api,kwargs=rig
    kwargs.update(extra)
    return study.run_card(directory,copy.deepcopy(api.doc),{'id':'target-response'},config(),**kwargs)


def test_complete_pair_returns_only_target_and_proposes_profile_without_adopting(rig):
    directory,clock,api,_=rig
    result=run(rig)
    assert [f for _,f in api.writes]==[300e12+20e6,300e12]
    assert result['restoration']=='starting_target_confirmed'
    assert result['status']=='succeeded'
    assert result['proposed_profile']['applied'] is False
    assert (directory/'target-response.html').exists()
    assert len(json.loads((directory/'target-response.json').read_text())['trials'])==2


def test_operator_target_is_never_restored_to_old_study_baseline(rig):
    directory,clock,api,_=rig
    def interfere():
        if clock.t>1000.5 and api.revision==1:
            api.doc['target']['frequency_hz']=300e12+50e6;api.revision+=1
    clock.callback=interfere
    with pytest.raises(study.Superseded):run(rig)
    assert len(api.writes)==1 and api.doc['target']['frequency_hz']==300e12+50e6
    assert json.loads((directory/'target-response.json').read_text())['restoration']=='skipped_newer_foreground_or_configuration'


def test_foreground_sweep_preempts_without_study_return_write(rig):
    directory,clock,api,_=rig
    def interfere():
        if clock.t>1000.5:api.extra=[dict(id='shot',status='running',set_id='test',cards=[{'operation':'sweep.servo_modulation'}])]
    clock.callback=interfere
    with pytest.raises(study.Superseded):run(rig)
    assert len(api.writes)==1


def test_user_cancel_after_confirmed_change_restores_own_checkpoint(rig):
    directory,clock,api,_=rig
    with pytest.raises(study.CardCancelled):run(rig,cancel=lambda:clock.t>1000.6)
    assert [f for _,f in api.writes]==[300e12+20e6,300e12]
    assert json.loads((directory/'target-response.json').read_text())['restoration']=='starting_target_confirmed'


def test_unknown_write_is_neither_replayed_nor_compensated(rig):
    directory,clock,api,_=rig
    original=api.put_configuration
    def lost(*a,**kw):original(*a,**kw);raise OSError('reply lost')
    api.put_configuration=lost
    with pytest.raises(OSError):run(rig)
    assert len(api.writes)==1
    assert json.loads((directory/'target-response.json').read_text())['restoration']=='skipped_unconfirmed_command_no_replay'


def test_database_unavailable_before_first_write_does_not_touch_target(rig):
    class Missing:
        def read(self,*_):raise OSError('unavailable')
    with pytest.raises(OSError):run(rig,history=Missing())
    assert not rig[2].writes


def test_plan_obeys_each_existing_target_step_and_never_enlarges_limits():
    clock=Clock();api=API(clock);p=config();p['offsets_hz']=[2e9]
    with pytest.raises(study.DeviceError,match='exceeds'):study.validate_laser(api.doc,p)
    from ldcon.core.operation_registry import admission_class
    assert admission_class(api.doc,{'operation':'measure.target_response'})=='proxy'


def test_actual_target_procedure_waits_for_live_reload_and_returns(rig):
    """Exercise the real target procedure, including its saved-revision handshake."""
    import time
    directory, clock, api, kwargs = rig
    parent = directory.parent/'parent'
    parent.mkdir()
    (parent/'laser-live.json').write_text(json.dumps(dict(supported=True)))
    original = api.put_configuration
    def accepted(*args, **kw):
        before = str(api.revision)
        reply = original(*args, **kw)
        (parent/('laser-reload-result-'+reply['revision']+'.json')).write_text(json.dumps(dict(
            request_id=reply['revision'], status='applied', revision_before=before, revision_after=reply['revision'],
            updated_unix_s=time.time(), applied=['target.frequency_hz'])))
        (parent/'lock-target.json').write_text(json.dumps(dict(updated_unix_s=time.time(),
            target_frequency_hz=api.doc['target']['frequency_hz'], ramp_active=False,
            ramped_target_hz=api.doc['target']['frequency_hz'], frequency_hz=api.doc['target']['frequency_hz'], single_mode=True)))
        return reply
    api.put_configuration = accepted
    result = run(rig, target_runner=study.lock_target.run_lock_target)
    assert result['status'] == 'succeeded'
    assert [f for _,f in api.writes] == [300e12+20e6,300e12]


def test_only_new_deck_cards_can_be_added_while_existing_jobs_run():
    from types import SimpleNamespace
    from ldcon.core.configuration import _check_active
    from ldcon.web.web_runtime import ConflictError
    old = dict(id='deck', interfaces={}, cards=[dict(id='lock', parameters={'gain':1})])
    runtime = SimpleNamespace(interface_repository=SimpleNamespace(read=lambda *_:dict(document=old)))
    active = [dict(read_only=False, laser={'id':'test'}, deck_id='deck')]
    new = copy.deepcopy(old)
    new['cards'].append(dict(id='target-response'))
    _check_active(runtime,active,'interface-decks','deck',new)
    for changed in (old, {**new, 'interfaces':{'new':'port'}},
                    {**new, 'cards':list(reversed(new['cards']))},
                    {**new, 'cards':[dict(id='lock',parameters={'gain':2}),new['cards'][1]]}):
        with pytest.raises(ConflictError):
            _check_active(runtime,active,'interface-decks','deck',changed)


def test_readiness_reports_missing_target_instead_of_exception():
    from ldcon.core.interface_schema import readiness, validate_deck
    card = dict(id='response',operation='measure.target_response',uses=[],parameters={})
    deck = dict(id='test',name='Test',schema_version=1,interfaces={},cards=[card])
    validate_deck(deck)
    result = readiness(dict(id='test',allow_output=True,interfaces={},devices={}),deck,card)
    assert not result['runnable']
    assert any('target' in x.lower() for x in result['blocked_reasons'])


def test_wait_deadline_crossed_during_checks_never_sleeps_negative(rig):
    directory,clock,api,kwargs=rig
    original=clock.__call__
    def ticking():
        clock.t+=.001
        return original()
    old_sleep=clock.sleep
    def sleep(seconds):
        assert seconds>=0
        old_sleep(seconds)
    kwargs.update(clock=ticking,sleep=sleep)
    result=run(rig)
    assert result['status'] in ('succeeded','inconclusive')


def test_tail_ingestion_wait_uses_source_cadence_not_coarse_gap():
    from ldcon.jobs.target_response import history_tail_complete
    h={'samples':[{'time_unix_s':i*.04} for i in range(96)]}
    assert not history_tail_complete(h,2.,4.,.2)
    h['samples'].extend({'time_unix_s':i*.04} for i in range(96,100))
    assert history_tail_complete(h,2.,4.,.2)
    h['integrity_errors']=['duplicate']
    assert not history_tail_complete(h,2.,4.,.2)
