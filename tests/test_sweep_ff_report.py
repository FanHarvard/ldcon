"""Report evidence cannot turn incomplete data into a qualified physical claim."""
import json

import pytest

from ldcon.jobs.sweep_ff_report import evidence, read_analysis, render_source, waveform


def document():
    return dict(points=dict(t_unix_s=[10., 10.1, 10.2, 10.3],
        wavelength_nm=[299792458e9/(300e12+v) if v is not None else None for v in (5e6,2e6,None,1e6)],
        error_hz=[5e6, 2e6, None, 1e6], error_fit_hz=[0., 0., 0., 0.]),
        source='influx', time_base='ESP32 source clock', completeness=dict(complete=False),
        levels=[dict(t_start_unix_s=10., t_end_unix_s=10.4, offset_hz=0.)],
        parameters=dict(reference=dict(lock_target_hz=300e12, f_ref_minus_target_hz=0.)))


def test_preserves_raw_unfitted_error_and_missing_count():
    data = waveform(document())
    assert data['input_samples'] == 4 and data['excluded_samples'] == 1
    assert [t for t,e in data['points']] == [10.,10.1,10.3]
    assert [e for t,e in data['points']] == pytest.approx([5e6,2e6,1e6],abs=.1)


def test_uses_canonical_target_and_never_the_reference_or_fitted_error():
    doc = document()
    doc['parameters']['reference']['f_ref_minus_target_hz'] = 4e6
    assert waveform(doc)['points'][0][1] == pytest.approx(5e6,abs=.1)
    doc['parameters'] = {}
    with pytest.raises(ValueError, match='Canonical'):
        waveform(doc)


def test_uses_canonical_target_from_wavelength_when_available():
    doc = document()
    doc['parameters']['reference'] = dict(lock_target_hz=300e12)
    doc['points']['wavelength_nm'] = [299792458e9/(300e12+2e6)]*4
    doc['points']['programmed_offset_hz'] = [1e6]*4
    doc['levels'][0]['offset_hz'] = 1e6
    assert waveform(doc)['points'][0][1] == pytest.approx(1e6, abs=.1)


def test_fitted_delay_cannot_hide_step_response():
    doc = document()
    doc['points'] = dict(t_unix_s=[9.9,10.,10.1,10.2,10.3,10.4],
        wavelength_nm=[299792458e9/300e12]*6,
        programmed_offset_hz=[0.]*6, level_index=[-1]*6, error_hz=[0.]*6, error_fit_hz=[0.]*6)
    doc['parameters']['delay_s'] = .4
    doc['levels'] = [dict(t_start_unix_s=10.,t_end_unix_s=10.4,offset_hz=20e6)]
    plotted = waveform(doc)
    assert [t for t,e in plotted['points']] == [10.,10.1,10.2,10.3]
    assert [e for t,e in plotted['points']] == pytest.approx([-20e6]*4,abs=.1)
    assert plotted['excluded_samples'] == 2


@pytest.mark.parametrize('times,errors', [([1,2],[3]), ([1,1],[2,3]), ([2,1],[2,3])])
def test_inconsistent_or_reordered_data_rejected(times, errors):
    doc = document()
    doc['points']['t_unix_s'] = times
    doc['points']['wavelength_nm'] = errors
    with pytest.raises(ValueError):
        waveform(doc)


def test_artifact_is_confined_and_symlinks_refused(tmp_path):
    (tmp_path/'raw.json').write_text(json.dumps(document()))
    assert read_analysis(tmp_path, 'raw.json')['source'] == 'influx'
    (tmp_path/'alias.json').symlink_to(tmp_path/'raw.json')
    for name in ('../outside.json', str(tmp_path/'raw.json'), 'alias.json'):
        with pytest.raises(ValueError):
            read_analysis(tmp_path, name)


def test_missing_trial_stays_unknown_and_no_credential_copied(tmp_path):
    result = dict(status='inconclusive', trials=[dict(id='trial_1', candidate=dict(id='c_1', secret='do not copy'),
        condition=dict(id='slow',levels_hz=[-1e6,1e6,1e6,-1e6],dwell_s=.5,repetitions=2),
        evaluation=dict(qualified=False,reasons=['incomplete']), analysis_file='missing.json')])
    records = evidence(tmp_path,result)
    assert records[0]['cycle_period_s'] == 1
    assert records[0]['cycle_frequency_hz'] == 1
    assert records[0]['qualified'] is False
    assert records[0]['completeness'] is None
    assert 'secret' not in records[0]['candidate']
    source = render_source(result, records, [''], '2026-10-28T00:00:00-04:00')
    assert 'inconclusive' in source and 'not qualified' in source
    assert r'trial\_1' in source and 'unavailable or invalid' in source


def test_cycle_period_uses_visits_not_unique_values(tmp_path):
    (tmp_path/'raw.json').write_text(json.dumps(document()))
    result = dict(trials=[dict(id='1', condition=dict(levels_hz=[0,1,2,1]*3,dwell_s=.2,repetitions=3),
        candidate={}, evaluation={}, analysis_file='raw.json')])
    record = evidence(tmp_path,result)[0]
    assert record['cycle_period_s'] == pytest.approx(.8)
    assert record['cycle_frequency_hz'] == pytest.approx(1.25)
    assert record['waveform']['excluded_samples'] == 1
    assert record['qualified'] is False
