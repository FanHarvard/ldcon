"""Distinct-source scalar aggregation; no clocks, devices or control writes."""
import copy
import math
import statistics
from ldcon.jobs.measurement_acquisition import source_eligible


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class WindowSamples:
    def __init__(self, parameters, resolve):
        self.p = parameters
        self.resolve = resolve
        self.reset()

    def reset(self):
        self.names = None
        self.since = None
        self.values = {}
        self.sources = {}

    def add(self, now, row, names, *, slow_after_s=None):
        names = tuple(names)
        if names != self.names:
            self.reset()
            self.names = names
        if self.since is None:
            self.since = now
        current_qualified=True
        for name in names:
            value = self.resolve(row, name)
            if not finite(value):
                current_qualified=False
                continue
            frequency=row.get('observation',{}).get('frequency',{})
            source = row.get('acquired_unix_s', frequency.get('acquired_unix_s', row.get('acquired_s',frequency.get('acquired_s'))))
            if name == 'dither_response' and (self.p['average_samples'] > 1 or slow_after_s is not None):
                source = row.get('dither_source_unix_s')
                if slow_after_s is not None:
                    start = row.get('dither_source_start_s')
                    if not finite(start) or not slow_after_s < start <= now:
                        current_qualified=False
                        continue
            if not source_eligible(source,previous=self.sources.get(name)):
                if not finite(source) or source != self.sources.get(name):
                    current_qualified=False
                continue
            self.sources[name] = source
            self.values.setdefault(name, []).append(value)
            self.values[name] = self.values[name][-self.p['average_samples']:]
        counts = {name: len(self.values.get(name, [])) for name in names}
        enough = all(n >= self.p['average_samples'] for n in counts.values())
        partial = now-self.since >= self.p['average_max_wait_s'] and all(counts.values())
        if not current_qualified or not enough and not partial:
            return None
        statistic = statistics.mean if self.p['average_statistic'] == 'mean' else statistics.median
        result = copy.deepcopy(row)
        result['values'] = {**result.get('values', {}),
                            **{name: statistic(self.values[name]) for name in names}}
        result['aggregation'] = dict(counts=counts, statistic=self.p['average_statistic'],
                                     partial=not enough, elapsed_s=now-self.since)
        self.values = {}
        self.since = None
        return result
