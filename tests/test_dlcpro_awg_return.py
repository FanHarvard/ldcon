"""Partial-sweep return never restores a superseded target or unknown write."""
import pytest

from ldcon.drivers.dlcpro import UncertainDLCWrite
from .test_dlcpro_awg_tracking import tracked, staircase, play
from .test_dlcpro_offline import rig


def partial(t):
    s, o, d = t['sweep'], t['owner'], t['driver']
    s.begin(staircase(), o.target_revision)
    # An acknowledged tracking residual at a nonzero plateau, after which
    # cleanup removes the ARC waveform. This is inside the admitted envelope.
    d.state['piezo'] = 69.
    o.instrument.confirmed_outputs[o.channel]['piezo'] = 69.
    return s, o, d


def test_known_partial_record_returns_base_before_capture_check(tracked):
    s, o, d = partial(tracked)
    result = s.finish('failed', 'verified write exceeded timing budget', resume=False)
    assert result['awg_restored'] and result['arc_restored'] and result['base_restored']
    assert result['base_return']['state'] == 'verified'
    assert len(result['base_return']['writes']) == 2
    assert d.state['piezo'] == 70. and o.mode == 'idle'
    assert not result['pi_resumed'] and not result['outputs_uncertain']


@pytest.mark.parametrize('reason', ['target', 'cancel', 'source', 'policy'])
def test_context_loss_skips_return_with_known_held_output(tracked, reason):
    s, o, d = partial(tracked)
    if reason == 'target': o.target_revision += 1; o.target += 1e6
    if reason == 'cancel': s.request_cancel(resume=False)
    if reason == 'source': tracked['rig']['source'].missing = True
    if reason == 'policy': o.policy['client_timeout_s'] += 1
    result = s.finish('failed', 'known partial record', resume=False)
    assert d.state['piezo'] == 69.
    assert not result.get('base_restored') and not result['pi_resumed']
    assert result['restored'] and not result['outputs_uncertain']


def test_unknown_return_write_retains_lease_and_does_not_send_next(tracked, monkeypatch):
    s, o, d = partial(tracked); original = d.write; writes = []
    def fail(path, value, **kw):
        if path.endswith('pc:voltage-set'):
            writes.append(value)
            raise UncertainDLCWrite('unknown return')
        return original(path, value, **kw)
    monkeypatch.setattr(d, 'write', fail)
    with pytest.raises(UncertainDLCWrite): s.finish('failed', 'partial record', resume=False)
    assert len(writes) == 1 and s.last['outputs_uncertain']
    assert o.instrument.channels[o.channel]['lease'] == o.lease


def test_expired_durable_intent_never_sends_stale_tracking(tracked):
    inst = tracked['owner'].instrument; original = inst.audit; expired = []
    def audit(row):
        original(row)
        if row['event'] == 'write_intent' and row['parameter'].endswith('pc:voltage-set') and not expired:
            expired.append(row['value'])
            tracked['rig']['clock'].sleep(.25)
    inst.audit = audit
    result = play(tracked)
    rejected = [r for r in tracked['rig']['events'] if r['event'] == 'write_reviewed_not_applied']
    assert rejected and not result['outputs_uncertain']
    assert not any(k.endswith('pc:voltage-set') and v == expired[0] for k, v in tracked['driver'].writes)
