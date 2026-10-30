"""Read-only configured optical quality metadata; never an owner command."""
import math
import time

FIELDS = ('side_peak_height_max', 'side_peak_prominence_max',
          'main_fwhm_fraction', 'near_main_peak_prominence_max')
GROUPS = ('single_mode_max',)


def extract(laser):
    """Omit ambiguous/invalid configuration instead of inventing a threshold."""
    invalid = dict(quality_thresholds_read_ok=0)
    if not isinstance(laser, dict) or not isinstance(laser.get('devices'), dict):
        return invalid
    policies=[]
    for device in laser['devices'].values():
        if not isinstance(device,dict) or not isinstance(device.get('control_settings',{}),dict):
            return invalid
        optical=device.get('control_settings',{}).get('optical',{})
        if not isinstance(optical,dict):
            return invalid
        policies.append(optical)
    policies = [p for p in policies if isinstance(p, dict) and any(p.get(g) for g in GROUPS)]
    if len(policies) != 1:
        return dict(quality_thresholds_read_ok=int(not policies))
    policy = policies[0]
    if type(policy.get('classification_enabled',False)) is not bool:
        return invalid
    result = dict(quality_thresholds_read_ok=1,
                  quality_classification_enabled=int(policy.get('classification_enabled') is True))
    for group in GROUPS:
        values = policy.get(group, {})
        if not isinstance(values, dict):
            return invalid
        for metric in FIELDS:
            if metric not in values:
                continue
            value = values[metric]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return invalid
            result['quality_'+group+'_'+metric] = value
    return result


def collect(service, laser_id):
    from ldcon.web.dashboard_snapshot import root_for, read, ID
    from ldcon.core.state_layout import config_file
    from ldcon.core.deck_yaml import DeckLoader
    import yaml
    if not ID.fullmatch(laser_id):
        raise ValueError('Invalid quality metadata laser identity')
    root = root_for(service)
    def parse(raw):
        try:
            return yaml.load(raw, Loader=DeckLoader)
        except yaml.YAMLError as error:
            raise ValueError('Invalid quality configuration') from error
    value = read(root, config_file(root, 'lasers', laser_id), {}, limit=256*1024, parser=parse)
    if not isinstance(value, dict) or value.get('id') != laser_id:
        return dict(quality_thresholds_read_ok=0)
    from ldcon.jobs.quality_baseline import safe_read, export_metrics, export_reference_metrics
    record = safe_read(root/'quality-baseline.json', laser_id=laser_id)
    now = time.time()
    return {**extract(value), **export_metrics(record, now), **export_reference_metrics(record, now)}
