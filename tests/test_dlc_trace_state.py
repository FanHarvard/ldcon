from copy import deepcopy
from ldcon.jobs.dlcpro_cards import trace_record

def source():
    return dict(mode='lock',pi_enabled=True,fault=None,observed_s=10.,target_frequency_hz=400e12,
        observation=dict(acquired_s=9.95,acquired_unix_s=100.,frequency_hz=400e12+1e6,
            degraded=False,metrics={'side_peak_height_max':.1}))

def test_fresh_lock_and_quality_are_independent():
    s=source();before=deepcopy(s)
    r=trace_record(s,tolerance_hz=2e6,max_sample_age_s=.2)
    assert r['state']=='locked' and r['locked'] and r['error_hz']==1e6
    assert r['source_acquired_unix_s']==100. and r['wavelength_nm']>0 and s==before
    s['observation']['degraded']=True
    r=trace_record(s,tolerance_hz=2e6,max_sample_age_s=.2)
    assert r['state']=='mode_degraded' and r['locked']

def test_tracking_missing_stale_and_fault_are_not_locked():
    s=source()
    assert trace_record(s,tolerance_hz=.5e6,max_sample_age_s=.2)['state']=='tracking'
    for observation in (None,{},dict(s['observation'],acquired_s=9.),dict(s['observation'],acquired_s=11.)):
        s['observation']=observation
        r=trace_record(s,tolerance_hz=2e6,max_sample_age_s=.2)
        assert r['state']=='waiting_for_measurement' and not r['locked']
    s=source();s['fault']='uncertain output'
    r=trace_record(s,tolerance_hz=2e6,max_sample_age_s=.2)
    assert r['state']=='failed' and not r['locked']

def test_idle_and_scan_are_never_reported_locked():
    for mode,state in (('idle','hold'),('scan','sweeping')):
        s=source();s['mode']=mode;s['pi_enabled']=False
        r=trace_record(s,tolerance_hz=2e6,max_sample_age_s=.2)
        assert r['state']==state and not r['locked']
