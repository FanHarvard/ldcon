"""Read-only analyses of common measurement records; never install a result."""
import math
import statistics


def frequency_points(result, parameter):
    points=[]
    for index, row in enumerate(result.get('steps',[result])):
        if row.get('status')!='succeeded':continue
        readback=(row.get('application') or {}).get('readback',{})
        x=readback.get(parameter)
        if isinstance(x,dict):x=x.get('value')
        f=row.get('summary',{}).get('wavelength',{}).get('statistics',{}).get('frequency_hz',{}).get('mean')
        if type(x) in (int,float) and type(f) in (int,float) and math.isfinite(x) and math.isfinite(f):
            points.append(dict(index=index,value=x,frequency_hz=f))
    return points


def local_slope(result, parameter, *, discontinuity_hz):
    if type(discontinuity_hz) not in (int,float) or not math.isfinite(discontinuity_hz) or discontinuity_hz<=0:
        raise ValueError('Explicit positive discontinuity threshold required')
    points=frequency_points(result,parameter)
    base=dict(parameter=parameter,points=points,adopted=False,qualification='local operational response; recorded competing-loop and FF context applies')
    if len(points)<3:return dict(base,status='inconclusive',reason='at least three measured points required')
    if any(abs(b['frequency_hz']-a['frequency_hz'])>discontinuity_hz for a,b in zip(points,points[1:])):
        return dict(base,status='inconclusive',reason='frequency discontinuity; do not fit across branches')
    x0=statistics.mean(p['value'] for p in points);f0=points[0]['frequency_hz']
    xs=[p['value']-x0 for p in points];ys=[p['frequency_hz']-f0 for p in points]
    denom=sum(x*x for x in xs)
    if denom==0:return dict(base,status='inconclusive',reason='no measured parameter span')
    slope=sum(x*y for x,y in zip(xs,ys))/denom;mean=statistics.mean(ys)
    residuals=[y-mean-slope*x for x,y in zip(xs,ys)]
    return dict(base,status='estimated',frequency_hz_per_unit=slope,
                rms_residual_hz=math.sqrt(statistics.mean(r*r for r in residuals)),
                reference_value=x0,reference_frequency_hz=f0+mean)


def branch_segments(result, parameter, *, discontinuity_hz):
    if type(discontinuity_hz) not in (int,float) or not math.isfinite(discontinuity_hz) or discontinuity_hz<=0:
        raise ValueError('Explicit positive discontinuity threshold required')
    groups=[];previous=None
    for point in frequency_points(result,parameter):
        # Missing/failed steps break continuity; never join across unknown effects.
        if previous is None or point['index']!=previous['index']+1 or abs(point['frequency_hz']-previous['frequency_hz'])>discontinuity_hz:
            groups.append([])
        groups[-1].append(point);previous=point
    return dict(schema='ldcon.measured-branches.v1',parameter=parameter,segments=groups,
                physical_mode_identity_verified=False,lock_admission=False,adopted=False)


def feedforward_estimate(source_fit, compensation_fit):
    """Static initial guess only; simultaneous dynamic validation is separate."""
    if any(f.get('status')!='estimated' for f in (source_fit,compensation_fit)):
        raise ValueError('Two qualified local slope estimates required')
    denominator=compensation_fit['frequency_hz_per_unit']
    if denominator==0:raise ValueError('Compensation sensitivity is zero')
    return dict(coefficient=-source_fit['frequency_hz_per_unit']/denominator,
                source_parameter=source_fit['parameter'],compensation_parameter=compensation_fit['parameter'],
                adopted=False,status='proposal',qualification='static slope ratio; combined transient and optical-quality validation required')
