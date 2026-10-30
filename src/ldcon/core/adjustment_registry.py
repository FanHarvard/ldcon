"""Laser-domain adjustment families and finite runtime trial plans.

Capability describes an implementation, never an authority grant. Physical
bounds remain in the laser owner; monitor grants choose finite comparisons.
"""
import copy
import math

PARAMETER_FAMILIES = {
    'slow_loop': {'tec_gain':'1', 'tec_dt_cap_s':'s', 'supervision_interval_s':'s', 'piezo_center_dac':'count'},
    'feedforward': {'current_per_temperature':'current-axis unit / temperature-axis unit'},
}
FAMILIES = frozenset({'current','pi',*PARAMETER_FAMILIES})
OBJECTIVES = frozenset({'frequency_rms_hz','pzt_activity_count_per_s','pzt_midrange_rms_count'})


def number(value, name):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError(name+' must be finite numeric data')
    return float(value)


def candidate(value, family=None):
    fields=set(PARAMETER_FAMILIES[family]) if family else set().union(*PARAMETER_FAMILIES.values())
    if not isinstance(value,dict) or not value or set(value)-fields:
        raise ValueError('Unknown or empty runtime adjustment candidate')
    for key,v in value.items():
        if number(v,key)<=0 and key not in SIGNED_PARAMETERS:raise ValueError(key+' must be positive')
    return copy.deepcopy(value)


def parameters(value, family, *, require_candidate=False):
    fields={'objective','window_s','min_samples','settle_s','max_sample_gap_s','timeout_s',
            'minimum_improvement_fraction','maximum_cadence_fraction','minimum_tec_excursion_v'}
    if family not in PARAMETER_FAMILIES or not isinstance(value,dict) or not fields<=value.keys() or set(value)-fields-{'candidate'}:
        raise ValueError('A complete finite parameter-comparison plan is required')
    p=copy.deepcopy(value)
    if p['objective'] not in OBJECTIVES:raise ValueError('Unknown runtime comparison objective')
    for key,low,high in [('window_s',0,3600),('max_sample_gap_s',0,60),('timeout_s',0,86400),
                         ('minimum_improvement_fraction',0,1),('maximum_cadence_fraction',0,1)]:
        if not low<number(p[key],key)<=high:raise ValueError('Invalid '+key)
    for key in ('settle_s','minimum_tec_excursion_v'):
        if not 0<=number(p[key],key)<=3600:raise ValueError('Invalid '+key)
    if type(p['min_samples']) is not int or not 3<=p['min_samples']<=100000:
        raise ValueError('At least three distinct samples are required')
    if p['timeout_s']<=2*(p['window_s']+p['settle_s']):raise ValueError('Trial timeout cannot cover baseline and candidate')
    if family=='feedforward' and p['minimum_tec_excursion_v']<=0:
        raise ValueError('Feedforward comparison requires a nonzero observed TEC excursion')
    if 'candidate' in p:p['candidate']=candidate(p['candidate'],family)
    elif require_candidate:raise ValueError('An explicit runtime candidate is required')
    return p


SIGNED_PARAMETERS=frozenset(('current_per_temperature',))


def owner_policy(device,family):
    settings=device.get('control_settings',{})
    if not isinstance(settings,dict):raise ValueError('Invalid owner control settings')
    families=settings.get('background_adjustments',{})
    if not isinstance(families,dict):raise ValueError('Runtime envelopes must be a mapping')
    raw=families.get(family)
    if not isinstance(raw,dict) or raw.get('enabled') is not True:
        raise ValueError('No approved '+family+' runtime envelope in this laser owner')
    if set(raw)!={'enabled','bounds','max_excursion'} or not isinstance(raw['bounds'],dict) or not raw['bounds'] or set(raw['bounds'])-set(PARAMETER_FAMILIES[family]):
        raise ValueError('Incomplete runtime adjustment envelope')
    for key,b in raw['bounds'].items():
        # Signed coefficients (the TEC->current feed-forward, human decision 2026-10-27: measured -0.07 V/V) may have
        # negative bounds; every other runtime parameter stays strictly positive.
        if not isinstance(b,list) or len(b)!=2 or not number(b[0],key)<number(b[1],key) or (key not in SIGNED_PARAMETERS and not 0<number(b[0],key)):
            raise ValueError('Invalid runtime parameter bounds: '+key)
    if not isinstance(raw['max_excursion'],dict) or set(raw['max_excursion'])!={'current','temperature'}:
        raise ValueError('Explicit physical return excursions are required')
    for key,value in raw['max_excursion'].items():
        if number(value,key)<0:raise ValueError('Negative physical excursion')
    return copy.deepcopy(raw)


def capabilities(operation,device=None):
    native=operation=='servo.native_current'
    supported=['pi'] if native else ['current','pi']
    details={}
    for family,fields in PARAMETER_FAMILIES.items():
        item=dict(supported=False,fields=dict(fields),runtime_only=True)
        try:
            if native:raise ValueError('No independent steady TEC/FF actuator in the native current-lock adapter')
            cfg=owner_policy(device or {},family)
            if family=='feedforward' and not (device or {}).get('control_settings',{}).get('steady_lock',{}).get('tec_current_feedforward'):
                raise ValueError('An existing commissioned TEC-to-current feedforward binding is required')
            item.update(supported=True,bounds=cfg['bounds'],max_excursion=cfg['max_excursion'])
            supported.append(family)
        except (ValueError,KeyError,TypeError) as error:item['reason']=str(error)
        details[family]=item
    return dict(supported_adjustments=supported,unsupported_adjustments=[f for f in ('current','pi','slow_loop','feedforward') if f not in supported],
                parameter_adjustments=details,
                pi=dict(strategy='validated_candidates',runtime_only=True,save_settings=False,
                        candidate_fields=['kp_count_per_nm','ki_count_per_nm_s','integral_limit_count'],
                        requires_owner_envelope=True,requires_return_budget=native,
                        completion_mode='deadline' if native else 'verified_handover',
                        requires_firmware=None if native else 'pi_handover v1'))
