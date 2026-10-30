"""DLC display semantics: real quantities, no native DAC or fake mode evidence."""
from pathlib import Path
from collections import Counter
from copy import deepcopy
import json

import pytest
import yaml

from ldcon.web.dashboard_state import display_state
from ldcon.web.grafana_dashboard import build_dashboard, parameters


ROOT = (Path(__file__).resolve().parent / 'support')


def dashboard():
    laser = yaml.safe_load((ROOT/'examples/dlcpro/thf685/laser.yaml').read_text())
    deck = yaml.safe_load((ROOT/'examples/dlcpro/deck.yaml').read_text())
    options = parameters(dict(parameters={}), dict(reference_wavelength_nm=685.45,
        datasource_uid='test', report_card_ids=['steady-lock', 'frequency-triangle', 'frequency-sine']))
    return build_dashboard(laser, options, deck=deck)


def test_dlc_physical_coordinates_are_distinct_and_optional_fields_stay_absent():
    doc = dashboard()
    panels = {p['title']: p for p in doc['panels']}
    for title, label, field in [
        ('Diode current (mA)', 'Measured current', 'hardware.current.measured'),
        ('Diode current (mA)', 'Bias setpoint', 'hardware.current.setpoint'),
        ('Diode current (mA)', 'Total setpoint (including FF)', 'hardware.current.total_setpoint'),
        ('Temperature (degC)', 'Measured temperature', 'hardware.temperature.measured'),
        ('Temperature (degC)', 'Temperature setpoint', 'hardware.temperature.setpoint'),
        ('PZT voltage (V)', 'Measured PZT voltage', 'hardware.piezo.measured'),
        ('PZT voltage (V)', 'PZT setpoint', 'hardware.piezo.setpoint'),
    ]:
        query = next(t['expr'] for t in panels[title]['targets'] if t['legendFormat']==label)
        assert field in query and 'ldcon_observed_s' in query
        assert 'tlast_over_time' in query and 'count by(laser)' in query
        assert panels[title]['fieldConfig']['defaults']['unit'] == 'none'
    text = json.dumps(doc)
    assert 'PZT control DAC' not in text and 'laser_servo_driver' not in text
    assert 'Current dither response' not in text and 'single_mode' not in panels
    assert 'Calculated PZT' not in text


def test_dlc_quality_is_guarded_and_shared_baselines_limits_are_distinct():
    doc = dashboard()
    panel = next(p for p in doc['panels'] if p['title'] == 'Position-free secondary peaks')
    for query in panel['targets'][:2]:
        assert 'observation.metrics.' in query['expr']
        assert 'observation.quality_valid' in query['expr'] and 'sample_age_s' in query['expr']
    assert any('quality_single_mode_max_side_peak_height_max' in t['expr'] for t in panel['targets'])
    assert any('quality_baseline_reference_side_peak_height_max' in t['expr'] for t in panel['targets'])
    styles = {v['value']['fill'] for o in panel['fieldConfig']['overrides'] for v in o['properties'] if v['id']=='custom.lineStyle'}
    assert {'solid','dash','dot'} <= styles
    colors = {v['value']['fixedColor'] for o in panel['fieldConfig']['overrides'] for v in o['properties'] if v['id']=='color'}
    assert '#5794F2' in colors and '#FF9830' in colors


def test_dlc_summary_and_layout_are_complete():
    doc = dashboard()
    titles = {p['title'] for p in doc['panels']}
    assert {'Latest wavelength (nm)', 'Latest frequency offset (MHz)', 'Mean wavelength (5 min, nm)',
            'Mean offset (5 min, MHz)', 'RMS offset (5 min, MHz)',
            'Activity — current', 'Activity — timeline', 'State'} <= titles
    ids = [p['id'] for p in doc['panels']]
    assert len(ids) == len(set(ids))
    for i, first in enumerate(doc['panels']):
        a = first['gridPos']
        for second in doc['panels'][i+1:]:
            b = second['gridPos']
            assert not (a['x'] < b['x']+b['w'] and b['x'] < a['x']+a['w']
                        and a['y'] < b['y']+b['h'] and b['y'] < a['y']+a['h'])


def test_dlc_layout_migration_preserves_queries_and_is_idempotent(monkeypatch):
    from ldcon.web import grafana_dlcpro
    align = grafana_dlcpro.align_detail
    with monkeypatch.context() as patch:
        patch.setattr(grafana_dlcpro, 'align_detail', lambda value: value)
        original = dashboard()
    before = deepcopy(original)
    after = align(original)
    assert original == before
    queries = lambda value: Counter(t['expr'] for p in value['panels'] for t in p.get('targets', []))
    retired = {'Applied controller commands (events/min)', 'Last recorded quality baseline — acquisition time', 'Latest quality baseline acquisition'}
    assert not retired & {p['title'] for p in after['panels']}
    # Measurement series survive; target expressions deliberately change.
    measured = lambda d: Counter(t['expr'] for p in d['panels'] for t in p.get('targets', []) if 'hardware.' in t['expr'])
    assert not measured(before) - measured(after)
    assert align(after) == after
    assert after['uid'] == before['uid'] and after['templating'] == before['templating']


def test_dlc_shared_sections_match_common_detail_coordinates():
    doc = dashboard()
    def panel(title, kind):
        return next(p for p in doc['panels'] if (p['title'],p['type']) == (title,kind))
    # Sr422's common structure: six summary cells, activity table, split
    # timelines, two wide optical plots, then four detail plots per row.
    assert [p['title'] for p in doc['panels'] if p['gridPos']['y']==0] == [
        'State', 'Latest frequency offset (MHz)', 'Measured diode current (mA)',
        'Measured temperature (degC)', 'Measured PZT voltage (V)', 'Latest wavelength (nm)']
    assert panel('Activity — current','table')['gridPos'] == dict(x=0,y=4,w=24,h=5)
    assert panel('State','state-timeline')['gridPos'] == dict(x=0,y=9,w=12,h=8)
    assert panel('Activity — timeline','state-timeline')['gridPos'] == dict(x=12,y=9,w=12,h=8)
    assert panel('Target wavelength and fixed-reference offsets','timeseries')['gridPos'] == dict(x=0,y=17,w=12,h=8)
    assert panel('Frequency offset from target (MHz)','timeseries')['gridPos'] == dict(x=12,y=17,w=12,h=8)
    assert [p['title'] for p in doc['panels'] if p['gridPos']['y']==25] == [
        'Diode current (mA)', 'Temperature (degC)', 'PZT voltage (V)']
    assert all(p['gridPos']['w']==8 for p in doc['panels'] if p['gridPos']['y']==25)
    assert max(p['gridPos']['y']+p['gridPos']['h'] for p in doc['panels']) == 45


def test_dlc_layout_rejects_unknown_panels_instead_of_dropping_them():
    from ldcon.web.grafana_dlcpro import align_detail
    doc = dashboard()
    extra = deepcopy(doc['panels'][0]);extra.update(id=999,title='Unreviewed new series')
    doc['panels'].append(extra)
    with pytest.raises(ValueError, match='Unknown panel'):
        align_detail(doc)


@pytest.mark.parametrize('operation,state,locked,expected', [
    ('servo.dlcpro_frequency','locked',True,3),
    ('servo.dlcpro_frequency','mode_degraded',True,6),
    ('sweep.dlcpro_frequency','sweeping',False,8),
])
def test_dlc_state_recognizes_frequency_control_without_claiming_mode_purity(operation,state,locked,expected):
    job = dict(id='dlc', status='running', updated_utc='2026-10-28T00:00:00+00:00',
        controller_state=state, cards=[dict(operation=operation)],
        latest=dict(locked=locked, recorded_unix_s=1793145600.))
    assert display_state([job], now=1793145600.)['code'] == expected


def test_runtime_target_history_overrides_saved_target_during_study():
    from ldcon.web.dlcpro_target_query import effective_target_query
    doc = dashboard()
    for title in ('Latest frequency offset (MHz)', 'Frequency offset from target (MHz)',
                  'Target wavelength and fixed-reference offsets'):
        panel = next(p for p in doc['panels'] if p['title'] == title)
        assert effective_target_query() in panel['targets'][0]['expr']
    query = effective_target_query()
    assert 'ldcon_target_frequency_hz' in query and 'ldcon_open_loop' in query
    assert 'max by(laser) (tlast_over_time' in query  # newest publication, not highest target
    assert 'count by(laser)' in query and 'unless on(laser)' in query
