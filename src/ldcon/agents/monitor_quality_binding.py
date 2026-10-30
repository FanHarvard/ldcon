"""Bind existing optical monitor rules to the current quality definition only."""
import copy
from ldcon.agents.adjustment_quality import is_dither, thresholds


def conditions(rules, document):
    current = thresholds(document)
    values = {p.rsplit('.', 1)[-1]:v for p, v in current.items()
              if '.single_mode_max.' in p}
    result = copy.deepcopy(rules)
    # The same canonical dither limit the adjustment QualityReview may revise
    # (lock device steady_lock.window.warning_response); ambiguous -> unbound.
    dither = [v for p, v in current.items() if is_dither(p)]
    dither_limit = dither[0] if len(dither) == 1 else None
    for rule in result:
        metric = rule['metric']
        if metric == 'dither_response' and type(dither_limit) in (int, float) and dither_limit > 0:
            # One dither quality threshold, also used by the display. The
            # window-search edge is a separate experiment boundary, not an
            # extra condition an agent may wait for before reviewing quality.
            fraction = rule['clear_below']/rule['above'] if rule['above'] else 1.
            rule.update(above=dither_limit, clear_below=dither_limit*fraction)
            continue
        if not rule['id'].startswith('quality-') or not metric.startswith('fringe_metrics.'):
            continue
        name = metric[len('fringe_metrics.'):]
        if name not in values:
            continue
        # Retain configured hysteresis and persistence; never alter frequency,
        # source freshness, safety, dither validity or intervention time budgets.
        fraction = rule['clear_below']/rule['above'] if rule['above'] else 1.
        rule.update(above=values[name], clear_below=values[name]*fraction)
    return result
