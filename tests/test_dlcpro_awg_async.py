"""Deterministic in-process concurrency; no network or hardware access."""
import threading

import pytest

from .test_dlcpro_offline import rig
from .test_dlcpro_awg import request, FakePort
from .test_dlcpro_awg_protocol import prepared


class UploadGate(FakePort):
    def __init__(self, entered, release, writes):
        super().__init__()
        self.entered, self.release, self.writes = entered, release, writes
        self.changed = False

    def arm(self, *args):
        self.checkpoint()
        self.changed = True
        self.writes.append('output_off_upload')
        self.entered.set()
        assert self.release.wait(3.), 'test did not release mocked upload'
        self.checkpoint()
        self.writes.append('output_on')
        return dict(trigger_source='EXT')

    def restore(self):
        self.writes.append('restore')
        self.changed = False
        super().restore()


def gated(rig, monkeypatch, tmp_path):
    o, p, call = prepared(rig, monkeypatch, tmp_path)
    o.awg_sweep.sweep.config['arm_timeout_s'] = 20.
    entered, release, writes = threading.Event(), threading.Event(), []
    port = UploadGate(entered, release, writes)
    o.awg_sweep.sweep.factory = lambda *args: port
    row = call('begin', request=request(), token='b'*32, target_revision=o.target_revision)
    assert row['state'] == 'preparing' and not row['armed']
    assert entered.wait(2.)
    return o, p, call, port, release, writes


def test_slow_upload_keeps_real_main_and_child_heartbeats_live(rig, monkeypatch, tmp_path):
    o, p, call, port, release, writes = gated(rig, monkeypatch, tmp_path)
    try:
        for _ in range(12):
            rig['clock'].sleep(.5)
            p.dispatch(dict(action='heartbeat', laser_id=o.laser_id, lease=p.client))
            assert o.last_client == rig['clock']()
            call('heartbeat', token='b'*32)
            o.tick()
        assert writes == ['output_off_upload']
        assert call('status', request_id='a'*32)['state'] == 'preparing'
        release.set()
        assert o.awg_sweep.prepared.wait(2.)
        assert call('status', request_id='a'*32)['ready']
        call('cancel', request_id='a'*32, token='b'*32)
        assert o.mode == 'lock' and not o.fault
    finally:
        release.set()
        o.awg_sweep.prepared.wait(2.)


def test_cancel_during_upload_never_enables_output_or_arc(rig, monkeypatch, tmp_path):
    o, p, call, port, release, writes = gated(rig, monkeypatch, tmp_path)
    try:
        state = call('cancel', request_id='a'*32, token='b'*32)
        assert state['cancel_requested'] and state['state'] == 'preparing'
        assert o.awg_sweep.active() and p.client == 'existing-main-lease'
        release.set()
        assert o.awg_sweep.prepared.wait(2.)
        result = call('result', request_id='a'*32)
        assert result['restored'] and result['verdict'] == 'cancelled'
        assert result['outputs_uncertain'] is False
        assert writes == ['output_off_upload', 'restore']
        assert not any(path.endswith(':external-input:enabled') and value
                       for path, value in rig['driver'].writes)
        assert o.mode == 'lock'
    finally:
        release.set()
        o.awg_sweep.prepared.wait(2.)


@pytest.mark.parametrize('missing', ['main', 'child'])
def test_real_missing_heartbeat_cancels_before_enable(rig, monkeypatch, tmp_path, missing):
    o, p, call, port, release, writes = gated(rig, monkeypatch, tmp_path)
    try:
        rig['clock'].sleep(2.1)
        if missing == 'main':
            call('heartbeat', token='b'*32)
            assert o.last_client < rig['clock']()
        else:
            p.dispatch(dict(action='heartbeat', laser_id=o.laser_id, lease=p.client))
        release.set()
        assert o.awg_sweep.prepared.wait(2.)
        result = call('result', request_id='a'*32)
        assert result['verdict'] == 'cancelled' and result['restored']
        assert writes == ['output_off_upload', 'restore']
        assert result['pi_resumed'] is (missing == 'child')
    finally:
        release.set()
        o.awg_sweep.prepared.wait(2.)


def test_main_stop_waits_without_blocking_status_or_releasing_lease(rig, monkeypatch, tmp_path):
    o, p, call, port, release, writes = gated(rig, monkeypatch, tmp_path)
    stopped, errors = threading.Event(), []
    accepted = threading.Event()
    original = o.awg_sweep.sweep.request_cancel
    def cancellation(**kwargs):
        result = original(**kwargs)
        accepted.set()
        return result
    monkeypatch.setattr(o.awg_sweep.sweep, 'request_cancel', cancellation)
    def stop():
        try:
            p.dispatch(dict(action='stop', laser_id=o.laser_id, lease=p.client))
        except Exception as error:
            errors.append(error)
        finally:
            stopped.set()
    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        assert accepted.wait(2.)
        # Taking the owner mutex is deterministic evidence that stop does not
        # wait for upload while holding the protocol lock.
        with o.mutex:
            assert call('status', request_id='a'*32)['state'] == 'preparing'
            assert p.client == 'existing-main-lease'
        release.set()
        assert stopped.wait(2.)
        assert not errors and o.mode == 'idle'
        assert call('result', request_id='a'*32)['pi_resumed'] is False
        assert writes == ['output_off_upload', 'restore']
    finally:
        release.set()
        stopper.join(2.)


def test_readonly_preflight_failure_has_durable_no_cleanup_result(rig, monkeypatch, tmp_path):
    o, p, call = prepared(rig, monkeypatch, tmp_path)
    before = list(rig['driver'].writes)
    rig['driver'].values['io:fine-2:value-act'] = float('nan')
    call('begin', request=request(), token='b'*32, target_revision=o.target_revision)
    assert o.awg_sweep.prepared.wait(2.)
    result = call('result', request_id='a'*32)
    assert result['verdict'] == 'failed' and result['restored']
    assert not result['physical_changed'] and not result['outputs_uncertain']
    assert rig['driver'].writes == before and o.mode == 'lock'
    assert call('cancel', request_id='a'*32, token='b'*32)['restored']
    with pytest.raises(ValueError, match='already recorded'):
        call('begin', request=request(), token='b'*32, target_revision=o.target_revision)


def test_unknown_upload_retains_port_and_never_replays(rig, monkeypatch, tmp_path):
    o, p, call = prepared(rig, monkeypatch, tmp_path)
    calls = []
    class Unknown(FakePort):
        def arm(self, *args):
            calls.append('upload')
            self.changed = True
            raise TimeoutError('unknown mocked write')
    port = Unknown()
    o.awg_sweep.sweep.factory = lambda *args: port
    call('begin', request=request(), token='b'*32, target_revision=o.target_revision)
    assert o.awg_sweep.prepared.wait(2.)
    result = call('result', request_id='a'*32)
    assert result['outputs_uncertain'] and not result['restored']
    assert o.fault and not port.closed and calls == ['upload']
    call('cancel', request_id='a'*32, token='b'*32)
    assert calls == ['upload']
