"""Integer-period context averaging for slow loops that run while a continuous triangle plays (2026-10-23).

Human decision (2026-10-23 ~04:40Z): "Put the continuous triangle in before the sweep tests. In that mode keep the TEC
following ON. Compute the context DAC over at least one period and an integer number of periods."

A slow loop (sr422: TEC centring on the PZT DAC; sr1092: PI-centre following on the native integral) must not see the
triangle. The record is periodic with an exactly known period (one AWG record, AWG clock). The mean of a periodic signal
over any window of exactly n whole periods does not depend on where the window starts, so the triangle (and the PI's
periodic answer to it) contributes only its period mean. Windows here are additionally ALIGNED to the detected playback
start (start + k*n*period), so consecutive windows tile the run without overlap and the result does not depend on when
the host happened to look. The caller removes the period mean of what the record itself commands (e.g. the PZT
feed-forward term mc*(mod_raw - baseline), or its known period mean), so an asymmetric triangle does not bias either.

Samples are irregular (owner polls 0.1-0.5 s); the mean is time-weighted (trapezoid, with the window edges interpolated).
A window is used only when it is covered: no gap longer than ``max_gap_fraction`` of one period (default 1/2) and never
longer than ``max_gap_s`` (default 1 s), first sample within that gap of the window start and last within it of the
window end. (1/8 period made every window of a triangle of a few seconds or less 'uncovered' at the 0.1-0.5 s owner
polls; with half a period and n whole periods the residual of the triangle in the mean stays small and averages out over
the n periods because the sample phases are not locked to the record.) An uncovered window
is reported (reason) and skipped: the slow loop HOLDS (no move on a partial window), the next aligned window is tried.

Fallback (documented in doc/card-system-design.md): period unknown (not positive/finite) or playback start unknown (not
detected yet) -> no context at all -> the slow loop holds. Never a plain time average while the triangle runs.
"""
import math


class PeriodAverager:
    def __init__(self, period_s, *, min_window_s=0., max_gap_fraction=.5, max_gap_s=1.):
        self.period_s = float(period_s) if period_s is not None else math.nan
        if not math.isfinite(self.period_s) or self.period_s <= 0:
            self.periods = None
        else:
            self.periods = max(1, int(math.ceil(float(min_window_s) / self.period_s - 1e-9)))
        self.max_gap = min(float(max_gap_s), float(max_gap_fraction) * self.period_s) if self.periods else 0.
        self.start = None
        self.samples = []            # (t, value), increasing t
        self.window_index = 0        # next window to evaluate (aligned: start + k*periods*period)
        self.last = None             # last completed result (dict)
        self.skipped = 0

    @property
    def window_s(self):
        return None if self.periods is None else self.periods * self.period_s

    def set_start(self, t0):
        """Playback start (the record's phase 0, from the ADC detection or the fire time). Windows align to it."""
        if self.start is None and t0 is not None and math.isfinite(float(t0)):
            self.start = float(t0)
            self.samples = [s for s in self.samples if s[0] >= self.start - self.max_gap]

    def add(self, t, value):
        if value is None or t is None or not math.isfinite(float(value)) or not math.isfinite(float(t)):
            return None
        t, value = float(t), float(value)
        if self.samples and t <= self.samples[-1][0]:
            return None
        self.samples.append((t, value))
        return self.evaluate()

    def _bounds(self, k):
        w = self.window_s
        return self.start + k * w, self.start + (k + 1) * w

    def evaluate(self):
        """Return the newest completed window result (or None). Older windows are dropped."""
        if self.periods is None or self.start is None or not self.samples:
            return None
        result = None
        while True:
            a, b = self._bounds(self.window_index)
            if self.samples[-1][0] < b:
                break
            mean, reason = self._mean(a, b)
            self.window_index += 1
            if mean is None:
                self.skipped += 1
                result = dict(ok=False, reason=reason, start_s=a, end_s=b, periods=self.periods, window_s=b - a)
            else:
                result = dict(ok=True, mean=mean, start_s=a, end_s=b, periods=self.periods, window_s=b - a)
                self.last = result
            keep_from = b - self.max_gap
            self.samples = [s for s in self.samples if s[0] >= keep_from]
        return result

    def _mean(self, a, b):
        pts = [s for s in self.samples if a - self.max_gap <= s[0] <= b + self.max_gap]
        inside = [s for s in pts if a <= s[0] <= b]
        if len(inside) < 2:
            return None, 'fewer than two samples in the window'
        before = [s for s in pts if s[0] < a]
        after = [s for s in pts if s[0] > b]
        seq = ([before[-1]] if before else []) + inside + ([after[0]] if after else [])
        if seq[0][0] > a + self.max_gap or seq[-1][0] < b - self.max_gap:
            return None, 'window edge not covered'
        if any(y[0] - x[0] > self.max_gap + 1e-12 for x, y in zip(seq, seq[1:])):
            return None, 'sample gap longer than %.3g s' % self.max_gap

        def at(t):
            if t <= seq[0][0]:
                return seq[0][1]
            if t >= seq[-1][0]:
                return seq[-1][1]
            for x, y in zip(seq, seq[1:]):
                if x[0] <= t <= y[0]:
                    return x[1] + (y[1] - x[1]) * (t - x[0]) / (y[0] - x[0])
            return seq[-1][1]
        knots = [(a, at(a))] + [s for s in seq if a < s[0] < b] + [(b, at(b))]
        area = sum((y[0] - x[0]) * (x[1] + y[1]) / 2 for x, y in zip(knots, knots[1:]))
        return area / (b - a), None


def triangle_period_mean(levels_hz, segment_s=None):
    """Period mean of the commanded offset of one record of linear one-step ramps 0 -> levels[0] -> ... -> 0
    (equal segment durations): the trapezoid mean of the vertex sequence with 0 at both ends."""
    pts = [0.] + [float(v) for v in levels_hz] + [0.]
    n = len(pts) - 1
    return sum((x + y) / 2 for x, y in zip(pts, pts[1:])) / n
