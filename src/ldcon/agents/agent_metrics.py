"""Per-laser diagnostic contracts and explicit recorded-data bindings.

Card declarations own physical meanings. This module selects them from a frozen
laser/deck and adds only allowlisted diagnostic settings; never device endpoints,
credentials, arbitrary configuration, live device reads, or guessed thresholds.
"""
import json
import math

from ldcon.core.card_reporting import collect_reports
from ldcon.core.interface_schema import digest

RETIRED = ('A', 'B', 'C', 'F2')
FIELDS = {
    'optical.frequency': ['frequency_hz', 'error_hz', 'acquired_s'],
    'optical.wavelength': ['wavelength_nm', 'acquired_s'],
    'controller.state': ['controller_state', 'locked', 'single_mode'],
    'control.current_axis': ['outputs.current'],
    'control.temperature_axis': ['outputs.temperature'],
    'control.piezo_dac': ['outputs.piezo'],
    'optical.single_mode': ['single_mode'],
    'optical.main_peak_width': ['fringe_metrics.main_fwhm_fraction'],
    'optical.secondary_peaks': ['fringe_metrics.side_peak_height_max', 'fringe_metrics.side_peak_prominence_max'],
    'response.current_dither': ['dither_response'],
    'response.dither_quality': ['dither_response_valid', 'dither_source_age_s', 'dither_source_unix_s',
                                'dither_fit_window_s', 'dither_sample_count', 'dither_analysis_status'],
    'electrical.current': ['current_measured_a', 'current_a'],
    'model.current_estimate': ['current_estimate_a'],
    'control.current_dac': ['dac_code', 'native_pi_active', 'native_input_quality'],
    'control.current_setpoint': ['current_setpoint_a'],
    'thermal.temperature': ['temperature_c'],
    'control.temperature_setpoint': ['temperature_setpoint_c'],
}
QUALITY_FIELDS = ['fringe_metrics.fringe_period_px', 'fringe_metrics.fringe_period_correlation',
                  'fringe_metrics.fringe_interval_coverage_loss', 'fringe_metrics.fringe_gap_mad_fraction',
                  'fringe_metrics.profile_noise_max', 'fringe_metrics.near_main_peak_prominence_max']
POLICY = ('Use this laser-specific metric contract before every diagnosis. Report the current dither response '
          'with its declared unit, valid-fit flag and source age, and position-free secondary-peak height '
          'and prominence with the configured limits when these quantities are declared. These are '
          'complementary diagnostics: dither response is local wavelength sensitivity, not mode purity; '
          'secondary peaks are normalized fringe proxies, not calibrated modal power. Never infer '
          'single-mode operation from dither alone. Check period, noise, coverage, main-peak width and '
          'near-main shoulders before interpreting side peaks. Missing or stale data is unknown, not '
          'zero or healthy. Thresholds below are existing laser settings, not permission to change them. '
          'Current recorded threshold/unit fields take precedence over this frozen startup snapshot; report mismatches. '
          'Fixed-window A/B/C and fringe fill F2 in historical logs are retired diagnostic fields. '
          'Do not rank current health, infer drift, or alert from them. If required current metrics '
          'are absent, explicitly report that evidence gap instead of substituting a retired field. '
          'Use laser_log_read for the listed recorded snapshots/telemetry; these are not fresh owner reads.')


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def build(laser, deck, *, card_parameters=None):
    reports = collect_reports(deck, laser, card_parameters=card_parameters)
    rows=[]
    for q in reports['available_quantities']:
        if q['id']=='optical.fringe_fill':
            continue
        semantic={k:q[k] for k in ('id','title','unit','meaning','constraints','card_ids','operations')}
        fields=FIELDS.get(q['id'],[])
        binding=dict(tool='laser_log_read',kind='snapshot',filename='readings.json',fields=fields,
                     unavailable='If a field is absent in this operation or snapshot, report unavailable; do not infer zero.')
        if q['id']=='controller.state':
            binding['filename']='progress.json'
        if q['id']=='response.current_dither':
            binding['validity']=dict(required_true='dither_response_valid',source_time='dither_source_unix_s',
                                     age='dither_source_age_s',unit_field='dither_response_unit')
            binding['threshold_fields']=['dither_warning_threshold','dither_edge_threshold']
        if q['id'] in ('optical.secondary_peaks','optical.main_peak_width','optical.single_mode'):
            binding['quality_fields']=QUALITY_FIELDS
        if q['id']=='controller.actions':
            binding=dict(tool='laser_log_read',kind='telemetry',event_kinds=['command_applied'],window='Use the exact requested since_unix_s/until_unix_s; page with next_after_id.')
        if q['id']=='optical.mode_hops':
            binding=dict(tool='laser_log_read',kind='telemetry',event_kinds=['ecdl_relock_hop_candidate','ecdl_response_boundary'],window='Use the exact requested window; candidate is not a confirmed hop without confirmed=true.')
        if q['id']=='thermal.temperature':
            binding['value_conversion']=dict(scale=1,offset=273.15,source_unit='degC',target_unit='K')
        row=dict(quantity=semantic,recorded_source=binding)
        if 'acquire.metrics' in q['operations']:
            row['acquired_metric_source']=dict(tool='laser_log_read',kind='telemetry',event_kind='metric_sample',quantity=q['id'],fields=['quantity','value','unit','quality','source_unix_s','acquired_unix_s','fresh'],provenance_event='metric_binding',note='Quantity definitions and driver query bindings are separate. Read the recorded binding provenance; do not query a device or invent a backend.')
        rows.append(row)
    priority=['response.current_dither','response.dither_quality','optical.secondary_peaks','optical.main_peak_width','optical.single_mode']
    rows.sort(key=lambda r:priority.index(r['quantity']['id']) if r['quantity']['id'] in priority else len(priority))
    optical=[];dither=[]
    for name,device in laser.get('devices',{}).items():
        settings=device.get('control_settings',{})
        cfg=settings.get('optical',{})
        if cfg:
            entry=dict(device=name,method=cfg.get('metric_method','unconfigured'),classification_enabled=cfg.get('classification_enabled',False))
            for key in ('period_reference_px','period_tolerance_fraction'):
                if _number(cfg.get(key)):entry[key]=cfg[key]
            for key in ('single_mode_max',):
                entry[key]={k:v for k,v in cfg.get(key,{}).items() if k not in RETIRED and _number(v)}
            optical.append(entry)
        profile=settings.get('steady_lock',{})
        if profile:
            entry=dict(device=name)
            for key in ('warning_response',):
                value=profile.get('window',{}).get(key)
                if _number(value):entry[key]=value
            dither.append(entry)
    return dict(schema_version=1,laser_id=laser['id'],deck_id=deck['id'],laser_configuration_revision=digest(laser),configuration_scope='Frozen startup laser/deck snapshot; compare actual recorded runtime values and freshness.',policy=POLICY,
                metrics=rows,optical_configuration=optical,dither_configuration=dither,
                retired_fields=['fringe_metrics.'+k for k in RETIRED],
                scope='Frozen configuration and recorded log bindings; no device inspection, secrets, or output authority.')


def instructions(manifest):
    return '\n\n## Current laser metric contract\n'+POLICY+'\n\n'+json.dumps(manifest,ensure_ascii=True,indent=2)+'\n'
