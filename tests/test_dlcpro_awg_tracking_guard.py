"""Signed combined envelopes and opt-in durable receipt integration."""
from copy import deepcopy
import json

import pytest

from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_awg_plan import compile_record, classify_policy_change
from ldcon.owners.dlcpro_awg_tracking_guard import admit, verify_base, interior
from ldcon.owners.dlcpro_awg_zero import corrected_proof
from .test_dlcpro_awg_tracking import tracked, staircase, play
from .test_dlcpro_awg import config, make, request, FakePort
from .test_dlcpro_offline import rig


@pytest.mark.parametrize('factor', [-.16, 0., .16])
@pytest.mark.parametrize('pzt_factor', [-1., 1.])
def test_whole_arc_and_zero_cleanup_fit_hardware_and_laser_bounds(tracked, factor, pzt_factor):
    o, s = tracked['owner'], tracked['sweep']
    o.policy['ff_factor_ma_per_v'] = factor; tracked['driver'].factor = factor
    s.config['pzt_v_per_input_v'] = pzt_factor
    o.policy['axes']['piezo']['bounds'] = [69.5, 71.5]
    o.policy['axes']['current']['bounds'] = [99.8, 100.2]
    hardware = deepcopy(o.instrument.hardware)
    hardware['axes']['piezo']['bounds'] = [69.7,71.3]
    _, proof = compile_record(s.config, request(), o.snapshot(), o.policy)
    result = admit(proof, o.snapshot(), o.policy, hardware, o.control)
    p = result['tracking']; tolerance = p['readback_tolerance_v']
    assert p['arc_envelope_v'][0] <= 0 <= p['arc_envelope_v'][1]
    for bound in p['base_command_bounds_v']:
        for displacement in (-tolerance, 0., tolerance):
            verify_base(bound+displacement, result)
    assert 69.7 <= min(p['pzt_envelope_v']) <= max(p['pzt_envelope_v']) <= 71.3
    assert 99.8 <= min(p['total_current_envelope_ma']) <= max(p['total_current_envelope_ma']) <= 100.2


def test_armed_zero_correction_recalculates_full_tracking_envelope(tracked):
    o, s = tracked['owner'], tracked['sweep']; snapshot = o.snapshot()
    _, proof = compile_record(s.config, request(), snapshot, o.policy)
    first = admit(proof, snapshot, o.policy, o.instrument.hardware, o.control)
    corrected = corrected_proof(s.config, first, o.policy, .002)
    final = admit(corrected, snapshot, o.policy, o.instrument.hardware, o.control)
    assert final['tracking']['arc_envelope_v'] == pytest.approx([-.1,.102])
    assert final['tracking']['pzt_envelope_v'][1] > first['tracking']['pzt_envelope_v'][1]
    assert final['software_pi_during_record']


def test_no_tracking_headroom_rejects_before_any_awg_or_arc_mutation(tracked):
    o, s, d = tracked['owner'], tracked['sweep'], tracked['driver']
    o.policy['axes']['piezo']['bounds'][1] = 70.1
    before = list(d.writes); calls = []
    s.factory = lambda *args: calls.append(args)
    with pytest.raises(ValueError, match='continuous safe'): s.begin(request(),o.target_revision)
    assert not calls and d.writes == before and s.last['physical_changed'] is False
    assert o.mode == 'lock' and not o.fault


def test_adc_level_tolerance_and_host_brackets_are_not_relaxed(tracked):
    s = tracked['sweep']; plan = request()['plan']
    session = dict(request={'plan':plan}, origin_target_hz=3e14,
                   trigger_bounds_s=[100.,100.05])
    good = dict(value_v=.1, started_s=100.6, completed_s=100.61)
    assert interior(session,s.config,good)['index'] == 1
    assert interior(session,s.config,dict(good,value_v=.10101)) is None
    assert interior(session,s.config,dict(good,started_s=100.52)) is None
    assert interior(session,s.config,dict(good,completed_s=100.91)) is None


def test_opt_in_is_not_an_unreviewed_operational_gain_change():
    before = dict(awg_sweep=config(), axes={})
    after = deepcopy(before); after['awg_sweep']['track_plateaus'] = True
    assert classify_policy_change(before,after).category == 'safety'


def test_attached_result_retains_tracking_proof_and_rows_without_new_protocol(rig,monkeypatch,tmp_path):
    from ldcon.jobs.dlcpro_cards import Protocol
    from ldcon.owners.dlcpro_awg_protocol import attach
    o, _, _ = make(rig,monkeypatch)
    device = deepcopy(rig['device']); device['control_settings']['awg_sweep'] = dict(config(),track_plateaus=True)
    protocol = Protocol(o,device); protocol.client = 'existing-main-lease'
    attach(protocol,tmp_path,port_factory=FakePort,sleep=rig['clock'].sleep)
    def call(action, **kw):
        return protocol.dispatch(dict(action='awg_sweep_'+action,laser_id=o.laser_id,**kw))
    call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    assert o.awg_sweep.prepared.wait(2.)
    active = call('status',request_id='a'*32)
    assert active['ready'] and active['tracking']['enabled']
    assert active['proof']['software_pi_during_record']
    call('cancel',request_id='a'*32,token='b'*32)
    result = call('result',request_id='a'*32)
    assert result['tracking_samples'] == [] and not result['tracking']['qualified']
    assert not result['tracking']['active'] and result['restored']
    assert 'tracking_samples' not in call('status',request_id='a'*32)
    assert protocol.client == 'existing-main-lease'
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize('field,value', [('unit','nm'),('unit',None),
    ('acquisition_known',False),('acquisition_known',None)])
def test_dwell_report_requires_explicit_hz_and_acquisition_evidence(tracked,field,value):
    from ldcon.owners.dlcpro_awg_tracking_report import summarize
    result = play(tracked,gain=1.,drift=0.)
    for row in result['samples']:
        if value is None: row.pop(field,None)
        else: row[field] = value
    report = summarize(result,tracked['owner'].policy,10e6)
    assert not report['qualified'] and all(r['samples'] == 0 for r in report['plateaus'])


def test_dwell_report_preserves_established_quality_flag_reporting(tracked):
    from ldcon.owners.dlcpro_awg_tracking_report import summarize
    result = play(tracked,gain=1.,drift=0.)
    for row in result['samples']: row['quality'] = 'source_quality_512'
    assert summarize(result,tracked['owner'].policy,10e6)['qualified']
