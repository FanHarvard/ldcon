"""Read historical log-watch parameters. New monitoring uses agent.monitor."""
import math


def parameters(card, overrides):
    defaults = dict(agent_card_id='', interval_s=300., lookback_s=600.,
                    sample_interval_s=1., prompt='')
    p = {**defaults, **card['parameters'], **overrides}
    if set(p) != set(defaults):
        raise ValueError('Unknown periodic agent review parameter')
    from ldcon.core.interface_schema import identifier
    if not isinstance(p['agent_card_id'], str):
        raise ValueError('Invalid resident agent card ID')
    if p['agent_card_id']:
        identifier(p['agent_card_id'])
    for name, lower, upper in (('interval_s', 1, 604800), ('lookback_s', 1, 604800),
                               ('sample_interval_s', .1, 10)):
        if (type(p[name]) not in (int, float) or not math.isfinite(p[name])
                or not lower <= p[name] <= upper):
            raise ValueError('Invalid periodic agent review ' + name)
    if not isinstance(p['prompt'], str) or len(p['prompt']) > 4000 or '\x00' in p['prompt']:
        raise ValueError('Invalid periodic review prompt')
    return p


def run(*args, **kwargs):
    raise ValueError("Retired card: use agent.monitor")
