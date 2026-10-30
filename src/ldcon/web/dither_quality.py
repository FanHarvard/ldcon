"""Read-only dither quality; never a control admission or action trigger."""
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def policy(laser):
    """Mirror the owner's published-fit lifetime from the frozen laser profile."""
    profiles = [d.get('control_settings', {}).get('steady_lock', {})
                for d in laser.get('devices', {}).values() if d.get('type') == 'ecdl.rig.owner']
    if len(profiles) != 1 or not profiles[0].get('dither'):
        return None
    p = profiles[0]; d = p['dither']
    values = (p.get('supervision_interval_s', 10.), d.get('window_s', 13.), d.get('delivery_delay_s', 3.))
    if not all(finite(x) and x >= 0 for x in values):
        return None
    return dict(max_age_s=max(2 * values[0], values[1] + values[2] + 2.))


def evaluate(row, configured, now):
    result = dict(state='unavailable', high=None, reason='No valid fresh dither fit',
                  value=None, threshold=None, unit=row.get('dither_response_unit'), valid_until_unix_s=None)
    if not configured:
        result.update(state='not_configured', reason='Dither quality is not configured for this controller')
        return result
    value = row.get('dither_response'); threshold = row.get('dither_warning_threshold')
    source = row.get('dither_source_unix_s'); budget = configured.get('max_age_s')
    # Threshold and value must share the owner's recorded physical unit. Missing
    # fits, invalid numbers and missing thresholds are unknown, never healthy.
    if not (row.get('dither_response_valid') is True and
            all(finite(v) for v in (value, threshold, source, budget)) and
            value >= 0 and threshold > 0 and budget > 0 and result['unit']):
        return result
    if not 0 <= now-source <= budget:
        result.update(state='stale', reason='Dither fit is stale or has a future source timestamp')
        return result
    high = value > threshold
    result.update(state='dither_degraded' if high else 'normal', high=high, value=value,
                  threshold=threshold, valid_until_unix_s=source+budget,
                  source_unix_s=source, age_s=now-source,
                  reason='Fresh dither response exceeds its warning threshold' if high else 'Fresh dither response is within its warning threshold')
    edge = row.get('dither_edge_threshold')
    if finite(edge):result['edge_exceeded'] = value > edge
    return result
