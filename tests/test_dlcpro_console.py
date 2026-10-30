import json
from pathlib import Path

import pytest
from ldcon.core.contracts import DeviceError
from ldcon.drivers.dlcpro import Console, DLCPro, DLCParameterError, UncertainDLCWrite


class Wire:
    def __init__(self, chunks):
        self.chunks=list(chunks);self.sent=[]
    def recv(self,n):
        item=self.chunks.pop(0) if self.chunks else b''
        if isinstance(item,Exception):raise item
        return item
    def sendall(self,raw):self.sent.append(raw)
    def close(self):pass


def test_connect_disables_console_echo_and_verifies_before_use(monkeypatch):
    import socket
    w=Wire([b'> ',b"(param-set! 'echo #f)\r\n0\r\n> ",b'#f\n> '])
    options=[]
    w.setsockopt=lambda *args:options.append(args)
    monkeypatch.setattr(socket,'create_connection',lambda *args,**kwargs:w)
    console=Console.connect('fixture.invalid')
    assert console.stream is w
    expected=[(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)]
    if hasattr(socket,'TCP_QUICKACK'):
        expected += [(socket.IPPROTO_TCP,socket.TCP_QUICKACK,1)]*3
    assert options==expected
    assert w.sent==[b"(param-set! 'echo #f)\n",b"(param-ref 'echo)\n"]


@pytest.mark.parametrize('replies',[[b'2\n> '],[b'Error: -3 no such parameter\n> '],
                                  [b'0\n> ',b'#t\n> '],[TimeoutError()]])
def test_echo_initialization_failure_closes_connection_without_output_commands(monkeypatch,replies):
    import socket
    w=Wire([b'> ']+replies);closed=[]
    w.setsockopt=lambda *args:None
    w.close=lambda:closed.append(True)
    monkeypatch.setattr(socket,'create_connection',lambda *args,**kwargs:w)
    with pytest.raises(DeviceError):Console.connect('fixture.invalid')
    assert closed==[True]
    assert all(b"'echo" in command for command in w.sent)


def test_fragmented_telnet_negotiation_and_echo():
    expr="(param-ref 'serial-number)"
    raw=b'\xff\xfb\x01\xff\xfd\x03\xff\xfa\x18abc\xff\xffx\xff\xf0DLC firmware\r\n> '
    w=Wire([bytes([b]) for b in raw]+[(expr+'\r\n"DLC PRO_042070"\r\n> ').encode()])
    d=DLCPro(Console(w))
    assert d.read('serial-number')=='DLC PRO_042070'
    assert w.sent==[b'\xff\xfe\x01',b'\xff\xfc\x03',(expr+'\n').encode()]


def test_negotiation_acknowledgments_do_not_loop():
    w=Wire([b'\xff\xfc\x01\xff\xfe\x03> ']);Console(w)
    assert not w.sent


def test_telnet_control_flood_is_bounded():
    w=Wire([b'\xff\xfb\x01'*30])
    with pytest.raises(DeviceError,match='framing bound'):Console(w,max_reply_bytes=20)


def test_truncated_telnet_greeting_fails():
    with pytest.raises(ConnectionError):Console(Wire([b'\xff\xfb']))


def test_echoed_write_status_and_readback():
    w=Wire([b'> ',b"(param-set! 'laser1:dl:pc:voltage-set 50)\r\n0\r\n> ",
            b"(param-ref 'laser1:dl:pc:voltage-set)\r\n50\r\n> "])
    assert DLCPro(Console(w)).write('laser1:dl:pc:voltage-set',50)==50
    assert len(w.sent)==2


def test_compact_pzt_write_preserves_order_status_and_readback_in_one_reply():
    w=Wire([b'> ',b'(0 12.5)\n> ',b'(0 50.0)\n> '])
    d=DLCPro(Console(w));d.verify_compact_write_support()
    assert d.write('laser1:dl:pc:voltage-set',50.,compact=True)==50.
    assert w.sent[-1] == b"(let ((status (param-set! 'laser1:dl:pc:voltage-set 50.0))) (list status (param-ref 'laser1:dl:pc:voltage-set)))\n"
    assert len(w.sent)==2  # one constants-only probe, one physical transaction


@pytest.mark.parametrize('reply',[b'(2 50)\n> ', b'(0 49)\n> ', b'(#f 50)\n> ',
                                  b'(0 nan)\n> ', b'(0 50 extra)\n> ', b'Error: bad\n> ',TimeoutError(),b''])
def test_compact_write_unknown_or_mismatch_is_never_replayed(reply):
    w=Wire([b'> ',b'(0 12.5)\n> ',reply]);d=DLCPro(Console(w));d.verify_compact_write_support()
    with pytest.raises(UncertainDLCWrite):d.write('laser1:dl:pc:voltage-set',50.,compact=True)
    assert len(w.sent)==2


def test_compact_write_requires_read_only_capability_probe():
    w=Wire([b'> ']);d=DLCPro(Console(w))
    with pytest.raises(DeviceError,match='capability'):
        d.write('laser1:dl:pc:voltage-set',50.,compact=True)
    assert not w.sent


@pytest.mark.parametrize('reply',[b"(param-set! 'x 1)\r\n2\r\n> ",TimeoutError(),b''])
def test_failed_write_never_replayed(reply):
    w=Wire([b'> ',reply]);d=DLCPro(Console(w))
    with pytest.raises(UncertainDLCWrite):d.write('x',1)
    assert w.sent==[b"(param-set! 'x 1)\n"]


def test_read_optional_only_swallows_explicit_missing():
    w=Wire([b'> ',b'Error: -3 no such parameter\n> ',b'Error: -20 unavailable on your system\n> ',b'Error: -1 denied\n> '])
    d=DLCPro(Console(w))
    assert d.read_optional('x') is None
    assert d.read_optional('x') is None
    assert d.read_optional('y') is None
    with pytest.raises(DLCParameterError):d.read_optional('z')
    assert len(w.sent)==3


@pytest.mark.parametrize('reply',[TimeoutError(),b'nonsense\n> '])
def test_optional_read_does_not_hide_bad_transport_or_data(reply):
    d=DLCPro(Console(Wire([b'> ',reply])))
    with pytest.raises(DeviceError):d.read_optional('x')
    assert not d.unavailable_parameters


def test_wrong_echo_is_not_removed():
    d=DLCPro(Console(Wire([b'> ',b"(param-ref 'wrong)\n1\n> "])))
    with pytest.raises(DeviceError):d.read('x')


def test_fw19_capture_snapshot_no_writes():
    replies=json.loads((Path(__file__).parent/'fixtures/dlcpro-fw19.json').read_text())['replies']
    class Replay(Wire):
        def sendall(self,raw):
            self.sent.append(raw)
            expr=raw.decode().strip()
            assert expr.startswith('(param-ref '),expr
            self.chunks.append(replies[expr].encode())
    w=Replay([b'DLCpro firmware, version 1.9.0.27a1cf0\r\n> ']);d=DLCPro(Console(w))
    assert d.identity(1)['firmware_version']=='1.9.0.27a1cf0'
    s=d.snapshot(1)
    assert s['piezo']['setpoint']==49.57
    assert s['piezo']['measured']==50.341796875
    assert s['piezo']['calculated_output'] is None
    assert not s['piezo']['calculated_output_available']
    assert s['current']['total_setpoint']!=s['current']['setpoint']
    assert s['ff_enabled'] is True and s['ff_factor_ma_per_v']==-.16
    assert s['feedforward_master'] is None
    d.snapshot(1)
    assert w.sent.count(b"(param-ref 'laser1:dl:pc:voltage-out)\n")==1


def test_modern_optional_values_preserved():
    d=DLCPro(Console(Wire([b'> ',b'50.2\n> ',b'50\n> '])))
    assert d.read_optional('laser1:dl:pc:voltage-out')==50.2
    assert d.read_optional('laser1:feedforward-master')==50
    assert not d.unavailable_parameters

@pytest.mark.parametrize('serial,fw,ok', [('S','1.9',True),('wrong','1.9',False),('S','3.2',False)])
def test_identity_pin_without_writes(serial,fw,ok):
    w=Wire([b'> ',b'"S"\n> ',b'"1.9"\n> ']);d=DLCPro(Console(w))
    if ok:assert d.verify_identity(serial,fw)['firmware_version']=='1.9'
    else:
        with pytest.raises(DeviceError):d.verify_identity(serial,fw)
    assert all(b'param-ref' in v for v in w.sent)


def test_target_without_admitted_range_is_not_ready():
    import yaml
    from ldcon.owners.dlcpro_deployment import commissioning_issues
    root=(Path(__file__).resolve().parent / 'support')
    laser=yaml.safe_load((root/'examples/dlcpro/laser.offline.yaml').read_text())
    config=json.loads((root/'examples/dlcpro/owner.offline.json').read_text())
    laser['target'].pop('allowed_change_hz')
    assert any('allowed target changes' in x for x in commissioning_issues(config,[laser]))


def test_reply_deadline_includes_fragmented_negotiation(monkeypatch):
    from ldcon.drivers import dlcpro_console
    times=iter([0.,0.,3.])
    monkeypatch.setattr(dlcpro_console.time,'monotonic',lambda:next(times))
    w=Wire([b'\xff\xfb\x01',b'hello\n> '])
    with pytest.raises(TimeoutError):Console(w,timeout_s=2.)


def test_inspector_rejects_non_read_commands():
    import os
    import subprocess
    import sys
    root=(Path(__file__).resolve().parent / 'support')
    code='''
from ops.inspect_dlcpro import ReadOnlyConsole
class Wire:
    def command(self,expression):return 'read accepted'
g=ReadOnlyConsole(Wire())
assert g.command("(param-ref 'fw-ver)")=='read accepted'
for expr,writing in [("(param-set! 'x 1)",False),("(exec 'x)",False),("(param-ref 'x)",True),("(param-ref 'x)\\n(param-set! 'x 1)",False)]:
    try:g.command(expr,writing=writing)
    except PermissionError:pass
    else:raise AssertionError('write bypass')
assert len(g.commands)==1
'''
    env=dict(os.environ,PYTHONPATH=str(root/'packages/ldcon/src')+os.pathsep+str(root/'packages/experiment-core/src'))
    subprocess.run([sys.executable,'-c',code],cwd=root,env=env,check=True,capture_output=True)
