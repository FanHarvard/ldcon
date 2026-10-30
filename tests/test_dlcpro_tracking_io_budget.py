"""Optional separately approved latency budget never admits stale measurements."""
import pytest

from ldcon.owners.dlcpro_awg_plan import configuration
from .test_dlcpro_awg_tracking import tracked, play, staircase
from .test_dlcpro_offline import rig


def test_bounded_slow_verified_write_continues_but_marks_gap_unqualified(tracked, monkeypatch):
    tracked['sweep'].config.update(tracking_io_budget_s=.4, adc_max_interval_s=.2)
    d = tracked['driver']; original = d.write; delayed = []; writes = []
    def slow(path, value, **kw):
        result = original(path, value, **kw)
        if path.endswith('pc:voltage-set'): writes.append(value)
        if len(writes) == 5 and not delayed:
            delayed.append(True); tracked['rig']['clock'].sleep(.25)
        return result
    monkeypatch.setattr(d, 'write', slow)
    result = play(tracked)
    assert result['playback_completed'] and result['restored'] and result['pi_resumed']
    assert tracked['sweep'].config['adc_max_interval_s'] == .2
    assert result['tracking_observation_gaps']
    affected = [r for r in result['tracking']['plateaus'] if not r['adc_continuity_confirmed']]
    assert affected and all(not r['qualified'] for r in affected)


def test_separate_budget_never_allows_a_stale_adc_read_bracket(tracked, monkeypatch):
    s = tracked['sweep']; s.config.update(tracking_io_budget_s=.4, adc_max_interval_s=.2)
    original = s._read; delayed = []
    def read():
        row = original()
        if s.session and s.session['state'] == 'playing' and not delayed:
            delayed.append(True); tracked['rig']['clock'].sleep(.21)
            row['completed_s'] = tracked['rig']['clock']()
        return row
    monkeypatch.setattr(s, '_read', read)
    result = play(tracked)
    assert 'original freshness budget' in result['stop_reason']
    assert not result['playback_completed'] and not result['outputs_uncertain']


@pytest.mark.parametrize('value', [.05, 0, -1, None, float('inf'), 30.])
def test_invalid_tracking_budget_is_rejected(tracked, value):
    with pytest.raises(ValueError): configuration(dict(tracked['sweep'].config, tracking_io_budget_s=value))


def test_wider_budget_increases_edge_guard_without_changing_poll_or_send_freshness(tracked):
    s = tracked['sweep']; s.config.update(tracking_io_budget_s=.4, adc_max_interval_s=.2)
    result = play(tracked)
    p = result['request']['plan']; left, right = result['trigger_bounds_s']
    rows = [r for r in result['tracking_samples'] if r.get('verified')]
    assert rows and result['playback_completed']
    for r in rows:
        edge = left+p['lead_s']+(r['index']+1)*p['dwell_s']
        assert r['write_started_s']+.4 < edge
        assert r['write_started_s']-r['adc_started_s'] < .2
    gaps = [b['started_s']-a['started_s'] for a,b in zip(result['adc'], result['adc'][1:])]
    assert max(gaps) < .14
