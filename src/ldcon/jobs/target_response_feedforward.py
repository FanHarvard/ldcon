"""Read-only target-feedforward identification; never changes calibration.

Endpoint DAC differences estimate a static scale candidate, not the transient
optimum. Optical source time and DAC receive time must not be regressed as if
simultaneous. Physical validation is a separate owner-admitted experiment.
"""
import hashlib
import json
import math
import statistics

from ldcon.jobs.target_response_analysis import C_NM


def configuration(laser):
    from ldcon.drivers.esp32_servo import channel_metadata
    controllers=[(key,d) for key,d in laser.get('devices',{}).items()
                 if d.get('type') in ('ecdl.rig.owner','esp32servocon.native_current.owner')]
    if len(controllers)!=1:return dict(status='unavailable',reason='ambiguous_controller')
    key,d=controllers[0];settings=d['control_settings']
    if d['type']=='ecdl.rig.owner':
        p=settings['steady_lock'];slope=p['wavelength_nm_per_count']
        enabled=p.get('target_feedforward_enabled',False)
        chunk=p.get('target_ramp_step_hz',100e6)
        scale=p.get('target_feedforward_scale',1.)
    else:
        p=settings['native_current'];cal=p['calibration'];dac=channel_metadata(p['dac'])
        slope=cal['wavelength_nm_per_a']*cal['current_a_per_v']*dac['voltage_step_v']/(1<<(16-dac['bits']))
        enabled=True;chunk=p['native'].get('target_ramp_step_hz',100e6)
        scale=p['native'].get('target_feedforward_scale',1.)
    if not math.isfinite(slope) or not slope:raise ValueError('Invalid target feedforward calibration')
    return dict(status='configured',device_id=key,owner_type=d['type'],enabled=enabled,
        wavelength_nm_per_count=slope,target_feedforward_scale=scale,
        scale_basis='dimensionless multiplier of commissioned target-offset formula',
        firmware_chunk_ceiling_hz=chunk,request_ceiling_hz=laser['target'].get('max_change_hz'),
        calibration_sha256=hashlib.sha256(json.dumps(p,sort_keys=True,allow_nan=False).encode()).hexdigest())


def capture(laser,context):
    """Fixed owner status/inspect reads; only numerical evidence leaves here."""
    from ldcon.agents.agent_owner_status import owner_read
    import time
    if context.get('status')!='configured':return dict(status='unavailable')
    d=laser['devices'][context['device_id']]
    try:
        if d['type']=='ecdl.rig.owner':
            status=owner_read(d,'status')
            if status.get('fault') or status.get('connected') is not True:
                return dict(status='unavailable',reason='owner_not_ready')
            raw=owner_read(d,'inspect');s=raw.get('snapshot',{});cfg=s.get('servo_config',{})
            outputs=s.get('outputs',{});fault=bool(raw.get('fault'));fresh=True
        else:
            raw=owner_read(d,'status');s=raw.get('sample') or {};cfg=s.get('servo_config',{})
            outputs={};fault=bool(raw.get('fault'));fresh=raw.get('fresh') is True and raw.get('read_ok') is True
        keys=('target','control_offset','pid_multiplier','pid_kp','pid_ki','pid_kd',
              'pid_integral_min','pid_integral_max','multiplier_control','multiplier_target')
        numeric=lambda v:type(v) in (int,float) and math.isfinite(v)
        thermal=raw.get('tec_context',{}).get('target_response_thermal')
        return dict(thermal=thermal,status='observed' if fresh and not fault else 'unavailable',observed_unix_s=time.time(),
            fresh=fresh,fault=fault,configuration={k:cfg[k] for k in keys if numeric(cfg.get(k))},
            outputs={k:outputs[k] for k in ('current','temperature') if numeric(outputs.get(k))},
            coverage='endpoint snapshots only; intermediate actuator changes not excluded')
    except (OSError,ValueError,KeyError,TypeError) as error:
        return dict(status='unavailable',error_type=type(error).__name__)


def identify(result):
    """Produce traceable estimates, rejecting confounded or incomplete endpoints."""
    context=result.get('feedforward_context',{});p=result['parameters'];rows=[]
    if context.get('status')!='configured' or context.get('enabled') is not True:
        return dict(status='inconclusive',reason='target_feedforward_context_unavailable',trials=[])
    slope=context['wavelength_nm_per_count']
    for leg in result['trials']:
        row=dict(index=leg['index'],status='inconclusive');rows.append(row)
        history=leg.get('history',{});samples=history.get('samples',[])
        if history.get('integrity_errors') or leg.get('analysis',{}).get('status')!='settled':
            row['reason']='unqualified_record';continue
        pre=leg.get('control_before',{});post=leg.get('control_after',{})
        if pre.get('status')!='observed' or post.get('status')!='observed':
            row['reason']='control_provenance_missing';continue
        keys=('pid_multiplier','pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max','multiplier_control','multiplier_target')
        if any(type(item.get('configuration',{}).get(k)) not in (int,float) or not math.isfinite(item['configuration'][k])
               for item in (pre,post) for k in (*keys,'target','control_offset')):
            row['reason']='incomplete_controller_provenance';continue
        if context.get('owner_type')=='ecdl.rig.owner' and any(type(item.get('outputs',{}).get(k)) not in (int,float)
                for item in (pre,post) for k in ('current','temperature')):
            row['reason']='incomplete_actuator_provenance';continue
        if (pre.get('configuration',{}).get('pid_multiplier')!=1 or
            any(pre.get('configuration',{}).get(k)!=post.get('configuration',{}).get(k) for k in keys)
            or pre.get('outputs')!=post.get('outputs')):
            row['reason']='controller_or_other_actuator_changed';continue
        t0,end=leg['requested_unix_s'],leg['record_end_unix_s'];duration=min(p['baseline_s'],p['evaluation_window_s'])
        # DAC belongs to received time, optical wavelength to source time.
        # Require both inside the stationary window, separated from the move.
        def window(a,b):
            return [s for s in samples if a<=s['time_unix_s']<=b and a<=s.get('received_unix_s',s['time_unix_s'])<=b
                    and type(s.get('dac_count')) in (int,float) and math.isfinite(s['dac_count'])]
        before=window(t0-duration,t0);after=window(end-duration,end)
        if min(len(before),len(after))<p['minimum_samples']:
            row['reason']='insufficient_endpoint_DAC_samples';continue
        if any(w[0]['time_unix_s']-a>p['max_gap_s'] or b-w[-1]['time_unix_s']>p['max_gap_s']
               or max(y['time_unix_s']-x['time_unix_s'] for x,y in zip(w,w[1:]))>p['max_gap_s']
               for w,a,b in ((before,t0-duration,t0),(after,end-duration,end))):
            row['reason']='endpoint_gap';continue
        for w,target in ((before,leg['previous_target_frequency_hz']),(after,leg['target_frequency_hz'])):
            errors=[C_NM/s['wavelength_nm']-target for s in w]
            if (abs(statistics.mean(errors))>p['mean_error_max_hz'] or
                math.sqrt(statistics.mean(x*x for x in errors))>p['rms_error_max_hz'] or
                (p['peak_error_max_hz'] is not None and max(map(abs,errors))>p['peak_error_max_hz'])):
                row['reason']='endpoint_frequency_bias_or_noise';break
        if row.get('reason'):continue
        means=[statistics.mean(s['dac_count'] for s in w) for w in (before,after)]
        noise=[statistics.stdev(s['dac_count'] for s in w) for w in (before,after)]
        predicted=(C_NM/leg['target_frequency_hz']-C_NM/leg['previous_target_frequency_hz'])/slope
        if abs(predicted)<max(4.,4*max(noise)):
            row['reason']='insufficient_actuator_excursion';continue
        ratio=(means[1]-means[0])/predicted
        if not math.isfinite(ratio) or ratio<=0:
            row['reason']='inconsistent_actuator_direction';continue
        row.update(status='static_estimate',scale=ratio,baseline_dac_mean=means[0],final_dac_mean=means[1],
                   endpoint_dac_stddev=noise,predicted_delta_count=predicted,
                   uncertainty_scale=2*sum(noise)/abs(predicted),samples=[len(before),len(after)])
    valid=[r for r in rows if r['status']=='static_estimate']
    if len(valid)<2 or not any(r['predicted_delta_count']>0 for r in valid) or not any(r['predicted_delta_count']<0 for r in valid):
        return dict(status='inconclusive',reason='need_qualified_outward_and_return_estimates',trials=rows)
    values=[r['scale'] for r in valid]
    return dict(status='static_candidate_only',target_feedforward_scale=statistics.median(values),
        observed_scale_range=[min(values),max(values)],trials=rows,applied=False,
        limitations='Static endpoint identification only. Endpoint snapshots cannot exclude intermediate motion. '
        'No candidate coefficient was physically tested. Dynamic optimality, future stability and safety authorization are unproven.')
