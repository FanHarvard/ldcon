from types import SimpleNamespace
import copy

from ldcon.owners import ecdl_optical
from ldcon.web import grafana_quality
from ldcon.web import quality_threshold_metrics
from ldcon.agents.adjustment_quality import thresholds
from ldcon.owners.ecdl_steady_policy import adjustment_ready
from ldcon.agents.monitor_quality_binding import conditions


def optical():
    return dict(interferometer='2', metric_method='phase_free_v1', classification_enabled=True,
        period_reference_px=100, mode_bands=[dict(id='desired', low_hz=1, high_hz=3)],
        single_mode_max=dict(side_peak_height_max=.3,side_peak_prominence_max=.15,main_fwhm_fraction=.2),
        warning_max=dict(side_peak_height_max=.5,side_peak_prominence_max=.4,main_fwhm_fraction=.4))


def test_classification_and_maintenance_have_one_limit(monkeypatch):
    metrics=dict(side_peak_height_max=.31,side_peak_prominence_max=.1,
                 main_fwhm_fraction=.1,profile_noise_max=.001,F2=.7)
    monkeypatch.setattr('ldcon.owners.fringe_peaks.analyze',lambda *a:dict(metrics=metrics,qualified=True))
    frame=SimpleNamespace(patterns=[SimpleNamespace(interferometer='2',pixels=[0,1,0],quality='valid')],
                          frequency=SimpleNamespace(frequency_hz=2))
    config=optical()
    result=ecdl_optical.classify(frame,config)
    assert result['single_mode'] is False and result['maintenance_required'] is True
    config.pop('warning_max')
    assert ecdl_optical.classify(frame,config)==result
    metrics['side_peak_height_max']=.3
    result=ecdl_optical.classify(frame,config)
    assert result['single_mode'] is True and result['maintenance_required'] is False


def test_monitor_and_metadata_use_the_same_quality_values():
    doc=dict(devices=dict(rig=dict(type='ecdl.rig.owner',control_settings=dict(
        optical=optical(),steady_lock=dict(window=dict(warning_response=.0024,edge_response=.003))))))
    rules=[dict(id='quality-height',metric='fringe_metrics.side_peak_height_max',above=.5,clear_below=.45),
           dict(id='dither-high',metric='dither_response',above=.003,clear_below=.0024)]
    bound=conditions(rules,doc)
    assert bound[0]['above']==.3 and bound[0]['clear_below']==.27
    assert bound[1]['above']==.0024
    # 2026-10-28: the dither quality limit is a revisable threshold; edge_response never is.
    assert all('.single_mode_max.' in p or p.endswith('.steady_lock.window.warning_response') for p in thresholds(doc))
    assert thresholds(doc)['devices.rig.control_settings.steady_lock.window.warning_response']==.0024
    assert not any('edge_response' in p for p in thresholds(doc))
    metadata=quality_threshold_metrics.extract(doc)
    assert metadata['quality_single_mode_max_side_peak_height_max']==.3
    assert not any('warning_max' in k for k in metadata)


def test_quality_labels_do_not_block_correction_but_explicit_guards_remain():
    obs=dict(frequency=dict(frequency_hz=2),mode_id='desired',single_mode=False,
             maintenance_required=True,metrics=dict(side_peak_height_max=.31))
    assert adjustment_ready(obs,{},2)
    p=dict(entry_gate=dict(mode_id='desired',branch_tolerance_hz=.1,
                          metrics_max=dict(side_peak_height_max=.4)))
    assert adjustment_ready(obs,p,2)
    obs['metrics']['side_peak_height_max']=.41
    assert not adjustment_ready(obs,p,2)
    obs['frequency']['frequency_hz']=None
    assert not adjustment_ready(obs,{},2)


def test_grafana_replaces_legacy_lines_and_keeps_distinct_metric_colors():
    ds=dict(type='prometheus',uid='vm')
    panels=[]
    for index,title in enumerate(('Position-free secondary peaks (1)','Main fringe peak width (1)','Current dither response (fm/mV)')):
        panels.append(dict(id=index+1,title=title,type='timeseries',datasource=ds,gridPos=dict(x=0,y=8*index,w=12,h=8),
            fieldConfig=dict(defaults={},overrides=[]),targets=[
                dict(refId='A',expr='raw',legendFormat='Response'),
                dict(refId='B',expr='warning',legendFormat='Warning threshold'),
                dict(refId='C',expr='edge',legendFormat='Boundary threshold')]
                if 'dither' in title else [dict(refId='A',expr='raw'),
                dict(refId='quality_warning_max_side_peak_height_max',expr='old',legendFormat='Height warning limit')]))
    old=dict(panels=panels)
    value=grafana_quality.apply(old)
    assert value==grafana_quality.apply(copy.deepcopy(value))
    for panel in value['panels']:
        assert not any('warning_max' in t.get('expr','') or t.get('legendFormat')=='Boundary threshold' for t in panel['targets'])
    side=value['panels'][0]
    limits=[t for t in side['targets'] if t['refId'].startswith('quality_')]
    baselines=[t for t in side['targets'] if t['refId'].startswith('baseline_')]
    assert len(limits)==2 and len(baselines)==2
    colors={o['matcher']['options']:next(p['value']['fixedColor'] for p in o['properties'] if p['id']=='color')
            for o in side['fieldConfig']['overrides']}
    assert colors['Height quality limit'] != colors['Prominence quality limit']
    assert value['panels'][-2]['title']=='Last recorded quality baseline — acquisition time'
    assert value['panels'][-1]['title']=='Latest quality baseline acquisition'
    assert all('ldcon_quality_baseline_reference_' in t['expr'] for t in baselines)
    assert 'previous reference retained' in str(value['panels'][-1]['fieldConfig'])


def test_retired_band_can_only_be_removed_through_live_authority():
    import pytest
    from ldcon.core.agent_authority import describe_changes, live_class
    from ldcon.owners.ecdl_projection import classify_policy
    before=dict(devices=dict(rig=dict(control_settings=dict(optical=optical()))))
    after=copy.deepcopy(before)
    after['devices']['rig']['control_settings']['optical'].pop('warning_max')
    rows=describe_changes('lasers',before,after)
    assert rows and all(r['class']=='free' and r['live']=='owner_hot' and r['removed'] for r in rows)
    assert all(live_class('lasers',tuple(r['path'].split('.'))) == 'owner_hot' for r in rows)
    assert classify_policy(before,after).category == 'quality'
    with pytest.raises(ValueError,match='retired'):
        describe_changes('lasers',after,before)


def test_near_main_metric_uses_configured_limit_not_hidden_zero(monkeypatch):
    metrics=dict(side_peak_height_max=.1,side_peak_prominence_max=.05,main_fwhm_fraction=.1,
                 near_main_peak_prominence_max=.003,profile_noise_max=.001,F2=.7)
    monkeypatch.setattr('ldcon.owners.fringe_peaks.analyze',lambda *a:dict(metrics=metrics,qualified=False,period_qualified=True))
    frame=SimpleNamespace(patterns=[SimpleNamespace(interferometer='2',pixels=[0,1,0],quality='valid')],frequency=SimpleNamespace(frequency_hz=2))
    config=optical();config['single_mode_max']['near_main_peak_prominence_max']=.005
    result=ecdl_optical.classify(frame,config)
    assert result['single_mode'] and not result['maintenance_required']
    metrics['near_main_peak_prominence_max']=.006
    result=ecdl_optical.classify(frame,config)
    assert not result['single_mode'] and result['maintenance_required']


def test_grafana_baseline_migration_keeps_existing_time_panel_id_and_layout():
    old=dict(panels=[dict(id=1,type='timeseries',title='Main fringe peak width (1)',
                         datasource={'uid':'vm'},targets=[],gridPos=dict(x=0,y=0,w=24,h=8)),
                     dict(id=42,type='stat',title='Last accepted quality baseline',
                          gridPos=dict(x=0,y=8,w=12,h=3))])
    result=grafana_quality.apply(old)
    time_panel=next(p for p in result['panels'] if p['id']==42)
    assert time_panel['gridPos']==old['panels'][1]['gridPos']
    assert time_panel['title']=='Last recorded quality baseline — acquisition time'
    assert 'reference_source_end_unix_s' in time_panel['targets'][0]['expr']
    assert result==grafana_quality.apply(result)
    assert len(result['panels'])==3
