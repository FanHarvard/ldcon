"""FF sweep orchestration attached to an existing native-current lease.

No second ESP/CLD connection is created. AWG changes are journaled before
execution; uncertain AWG writes retain the AWG lease. FF restoration is always
handled separately, and its outcome never implies AWG restoration.
"""
from contextlib import contextmanager
import fcntl
import copy
import json
import math
import os
from pathlib import Path
import threading
import time

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.drivers.siglent import AWGChannel, AWGStateMismatch, fields, number
from ldcon.core.instrument_client import BrokerError, BrokerTransport


def compile_wave(plan, frequency_hz_per_v, *, sample_rate_hz=20000., max_samples=8_000_000):
    """Compile exactly the requested cycles, with centre lead/tail and ramps."""
    import numpy as np
    numeric = ('half_span_hz', 'step_hz', 'dwell_s', 'edge_s', 'cycles')
    for key in numeric:
        value = plan.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Positive finite sweep value required: ' + key)
    if (type(frequency_hz_per_v) not in (int, float) or not math.isfinite(frequency_hz_per_v)
            or not frequency_hz_per_v):
        raise ValueError('Approved signed frequency_hz_per_v is required')
    if not 0 < sample_rate_hz <= 1e6 or not math.isfinite(sample_rate_hz):
        raise ValueError('Invalid sample rate')
    n = round(plan['half_span_hz'] / plan['step_hz'])
    per, edge = round(plan['dwell_s'] * sample_rate_hz), round(plan['edge_s'] * sample_rate_hz)
    if (n < 1 or not math.isclose(n * plan['step_hz'], plan['half_span_hz'], abs_tol=1e-6)
            or per < 2 or not 1 <= edge <= per
            or not math.isclose(per / sample_rate_hz, plan['dwell_s'], abs_tol=1e-12)
            or not math.isclose(edge / sample_rate_hz, plan['edge_s'], abs_tol=1e-12)
            or int(plan['cycles']) != plan['cycles'] or not 1 <= plan['cycles'] <= 10000):
        raise ValueError('Sweep timing, span or cycles are not representable')
    explicit = plan.get('levels')
    if explicit:
        # labscript record: lead at the centre, the requested levels in order, tail at the centre.
        lead = round(float(plan.get('lead_s', 0.)) * sample_rate_hz)
        tail = max(round(float(plan.get('tail_s', 0.)) * sample_rate_hz), edge)   # the return ramp needs room
        steps = [float(v) / plan['step_hz'] for v in explicit]
        count = lead + len(steps) * per + tail
        pad = (-count) % 16
        count += pad
        if count < 32 or count > max_samples:
            raise ValueError('ARB length must fit the device')
        samples = np.empty(count, dtype=float)
        samples[:lead] = 0.
        cursor, previous = lead, 0.
        for level in steps:
            value = level * plan['step_hz'] / frequency_hz_per_v
            samples[cursor:cursor + edge] = np.linspace(previous, value, edge + 1)[1:]
            samples[cursor + edge:cursor + per] = value
            previous, cursor = value, cursor + per
        samples[cursor:cursor + edge] = np.linspace(previous, 0., edge + 1)[1:]
        samples[cursor + edge:] = 0.
        amplitude = max(.002, float(np.max(samples) - np.min(samples)))
        codes = np.rint(samples / (amplitude / 2) * 32767).astype('<i2')
        return dict(codes=codes, amplitude_vpp=amplitude, offset_v=0.,
                    min_v=float(np.min(samples)), max_v=float(np.max(samples)),
                    duration_s=count / sample_rate_hz, sample_rate_hz=sample_rate_hz,
                    cycles=1, centre_at_both_ends=True, explicit_levels=len(steps), lead_s=lead / sample_rate_hz,
                    tail_s=(tail + pad) / sample_rate_hz)
    # Start at centre, visit both endpoints, return to centre. Never join
    # independent sweeps across a missing endpoint or a discontinuous wrap.
    levels = ([0] + list(range(-1, -n - 1, -1)) + list(range(-n + 1, n + 1))
              + list(range(n - 1, -1, -1)))
    count = len(levels) * per * int(plan['cycles'])
    if count < 32 or count > max_samples or count % 16:
        raise ValueError('ARB length must fit the device and be a multiple of 16')
    samples = np.empty(count, dtype=float)
    cursor, previous = 0, 0.
    for _ in range(int(plan['cycles'])):
        for level in levels:
            value = level * plan['step_hz'] / frequency_hz_per_v
            samples[cursor:cursor + edge] = np.linspace(previous, value, edge + 1)[1:]
            samples[cursor + edge:cursor + per] = value
            previous, cursor = value, cursor + per
    amplitude = max(.002, float(np.max(samples) - np.min(samples)))
    codes = np.rint(samples / (amplitude / 2) * 32767).astype('<i2')
    return dict(codes=codes, amplitude_vpp=amplitude, offset_v=0.,
                min_v=float(np.min(samples)), max_v=float(np.max(samples)),
                duration_s=count / sample_rate_hz, sample_rate_hz=sample_rate_hz,
                cycles=int(plan['cycles']), centre_at_both_ends=True)


def configuration(laser):
    """Resolve one explicitly approved AWG route; never guess a physical owner."""
    device = laser['devices'][laser['interfaces']['current_modulation']['device']]
    settings = device.get('control_settings', {}).get('native_feedforward_awg')
    required = {'profile', 'expected_identity', 'load', 'reference', 'centre_tolerance_v'}
    optional = {'rest_v'}
    # No coupling attestation route (2026-10-22): the session verifies COUP? itself through the broker's
    # channel_environment action (instruments runtime 2026-10-22), which also gates every write on the lease.
    # rest_v: the channel's resting DC level outside a sweep (shared instrument: another owner's reviewed
    #         evidence records this channel at rest); the session raises it to the centre at prepare and
    #         returns it at restore.
    if not isinstance(settings, dict) or not required <= set(settings) <= required | optional:
        raise ValueError('Explicit native_feedforward_awg configuration is required')
    if 'rest_v' in settings and (type(settings['rest_v']) not in (int, float) or not math.isfinite(settings['rest_v'])):
        raise ValueError('rest_v must be a finite voltage')
    connection, conditions = device['connection'], device['absolute_conditions']
    channel = connection['channel']
    if (not laser['allow_output'] or channel not in (1, 2)
            or conditions.get('preserve_channels') != [3 - channel]
            or settings['load'] != 'HZ' or settings['reference'] != 'initial_offset'
            or not isinstance(settings['expected_identity'], str) or not settings['expected_identity'].strip()):
        raise ValueError('AWG output permission, exact identity, HZ and reserved channel are required')
    low, high = conditions['modulation_v']['bounds_v']
    vpp = conditions['modulation_v']['max_vpp_v']
    tolerance = settings['centre_tolerance_v']
    if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (low, high, vpp, tolerance))
            or not low < high or vpp <= 0 or tolerance <= 0):
        raise ValueError('Approved AWG voltage bounds and centre tolerance are required')
    return dict(settings, channel=channel, resource=connection['resource'],
                broker=dict(endpoint=connection['endpoint'], instrument=connection['instrument'],
                            projection=copy.deepcopy(connection.get('projection', {}))),
                min_v=low, max_v=high, max_vpp=vpp, role='current_modulation')


class CheckpointTransport:
    """Check stop/fault between individual RPCs, never replay an in-flight RPC."""

    def __init__(self, transport, checkpoint):
        self.transport, self.checkpoint = transport, checkpoint

    def __getattr__(self, key):
        return getattr(self.transport, key)

    def channel_environment(self):
        self.checkpoint()
        return self.transport.channel_environment()

    def query(self, command):
        self.checkpoint()
        return self.transport.query(command)

    def command(self, command):
        self.checkpoint()
        return self.transport.command(command)

    def write_raw_and_wait(self, data):
        self.checkpoint()
        return self.transport.write_raw_and_wait(data)

    def close(self):
        return self.transport.close()


class NativeFFAwg:
    """ARB adapter for the existing channel broker, with fail-closed cleanup."""

    def __init__(self, config, *, factory=BrokerTransport, journal=lambda *_: None, authority=None):
        self.config, self.factory, self.journal = config, factory, journal
        self.authority = authority
        self.channel = self.before = None
        self.changed = self.uncertain_output = False
        self.name = self.wave = self.resolved = None
        self.checkpoint = lambda: None
        self.cleanup_checkpoint = lambda: None
        self.cleaning = False
        self.parking = False

    def set_checkpoint(self, checkpoint, cleanup_checkpoint=None):
        self.checkpoint = checkpoint
        self.cleanup_checkpoint = cleanup_checkpoint or (lambda: None)

    def _checkpoint(self):
        if self.parking:
            # The centre park and its read-back are SDG-only actions through the AWG lease (instruments service); they
            # never depend on the native owner. 2026-10-22 17:26:58Z the owner faulted, the native lease keepalive then
            # failed, and the checkpoint refused every park command, leaving CH2 armed ARB (v4.4).
            return
        if not self.cleaning:
            self.checkpoint()
        else:
            self.cleanup_checkpoint()

    def prepare(self, wave, centre_v):
        if self.channel is not None:
            raise ValueError('AWG adapter already owns a channel')
        self._checkpoint()
        io = CheckpointTransport(self.factory(self.config, owner='native FF sweep'), self._checkpoint)
        self.channel = AWGChannel(io, self.config)
        try:
            # Mandatory: an instruments runtime without channel_environment refuses here (BrokerError 'action').
            environment = io.channel_environment()
            self.journal('awg_environment', dict(identity=str(environment.get('identity')).strip(), coupling=environment.get('coupling'),
                                                 broker_ok=environment.get('ok'), reasons=environment.get('reasons')))
            if environment.get('ok') is False:
                raise ValueError('Broker refused the AWG environment: ' + '; '.join(environment.get('reasons') or []))
            if environment['identity'].strip() != self.config['expected_identity'].strip():
                raise ValueError('AWG identity/serial differs from approved identity')
            raw_coupling = environment['coupling'].strip()
            if raw_coupling.startswith('COUP '):
                raw_coupling = raw_coupling.split(' ', 1)[1]
            coupling = fields(raw_coupling)
            if any(coupling.get(key) != 'OFF' for key in ('TRACE', 'FCOUP', 'PCOUP', 'ACOUP')):
                raise ValueError('All required coupling/tracking fields must explicitly be OFF')
            self.before = self.channel.snapshot()
            if self.before['idn'].strip() != environment['identity'].strip():
                raise ValueError('AWG identity changed during preparation')
            if self.before['wave']['WVTP'] != 'DC':
                raise ValueError('First native FF sweep requires a static DC centre')
            actual = number(self.before['wave']['OFST'])
            rest_v = self.config.get('rest_v')
            at_rest = rest_v is not None and abs(actual - rest_v) <= self.config['centre_tolerance_v'] and abs(centre_v - rest_v) > self.config['centre_tolerance_v']
            if not at_rest and (abs(actual - centre_v) > self.config['centre_tolerance_v']
                                or not self.config['min_v'] <= actual <= self.config['max_v']):
                raise ValueError('AWG DC level differs from approved centre (and is not the configured rest level)')
            self.wave = wave
            # the static DC the channel holds before arm/play: the raised centre, or the existing level at the centre
            self.centre_text = f'{centre_v:.6g}' if at_rest else self.before['wave']['OFST']
            reference = self.before
            if at_rest:
                reference = copy.deepcopy(self.before)
                reference['wave']['OFST'] = f'{centre_v:.6g}'
            self.resolved = self.channel.resolve(wave, reference)
            if self.authority is not None:
                channel = self.config['channel']
                cleanup = []
                # Exact, separately admitted DC park commands; no trigger/upload retry.
                for command in ('BSWV WVTP,DC', 'BTWV STATE,OFF',
                                'BSWV OFST,' + self.centre_text,
                                'BSWV OFST,' + self.before['wave']['OFST'], 'OUTP OFF'):
                    cleanup.append(dict(channel=channel, operations=[
                        dict(op='write', data='C%d:%s' % (channel, command)), dict(op='query', data='*OPC?')]))
                io.configure_projection(self.authority, dict(kind='native_ff_awg',
                    channels={str(channel): dict(bounds=[self.config['min_v'], self.config['max_v']],
                        max_vpp=self.config['max_vpp'], axis='modulation', rest_v=self.config.get('rest_v'))},
                    max_points=len(wave['codes']), cleanup_operations=cleanup))
            if at_rest:
                # Raise the channel from its rest level to the sweep centre (DC only; output/load untouched).
                # The snapshot taken above is the rest state, so close() returns the channel to rest.
                if not self.config['min_v'] <= centre_v <= self.config['max_v']:
                    raise ValueError('Sweep centre is outside the approved voltage bounds')
                self.journal('awg_write_intent', dict(stage='centre', rest_v=rest_v, centre_v=centre_v))
                self.uncertain_output = True
                self.channel.command(f'BSWV WVTP,DC,OFST,{centre_v:.6g}')
                back = fields(self.channel.query('BSWV?'))
                if back.get('WVTP') != 'DC' or abs(number(back['OFST']) - centre_v) > self.config['centre_tolerance_v']:
                    raise ValueError('AWG DC centre readback mismatch: ' + str(back))
                self.uncertain_output = False
                self.changed = True        # v4.17: a raised centre is a change -> close() returns CH2 to the rest
                self.journal('awg_write_verified', dict(stage='centre', centre_v=centre_v, readback=back))
        except BaseException:
            # These are read-only failures, unless the owner itself reports a
            # quarantined stream. No waveform programming has been attempted.
            io.close()
            self.channel = None
            raise

    def stage(self):
        """Upload while FF is disabled. Successful upload is not playback."""
        self.journal('awg_write_intent', dict(stage='upload', before=self.before))
        self.uncertain_output = True
        metadata = dict(duration_s=self.wave['duration_s'], sample_rate_hz=self.wave['sample_rate_hz'])
        self.name = self.channel.upload(self.wave, metadata, self.resolved, 'a')
        self.changed = True
        self.journal('awg_write_verified', dict(stage='upload', name=self.name))
        # SDG2042X (2026-10-22 R1 on v2, 16:19Z): WVDT selects the uploaded record and plays it CONTINUOUSLY at once
        # (the BK does not). The ADC then swung +-1170 counts around the centre and the owner's FF start gate ("Fresh
        # stable physical ADC center samples") refused for 30 s. Park the channel back on the static DC centre; arm()/
        # play() select the uploaded record by name later.
        centre = self.centre_text
        if not self.channel.parked_dc(centre):
            self.journal('awg_write_intent', dict(stage='park', name=self.name, centre=centre,
                                                  reason='upload selected the record (continuous playback)'))
            self.channel.park_dc(centre)
            self.journal('awg_write_verified', dict(stage='park', centre=centre))
        self.uncertain_output = False

    def playing(self):
        """True when CH2 plays a record continuously (ARB with burst OFF) or cannot be read: the state that modulates
        the laser through the FF multipliers and keeps the owner's unwind from seeing agreeing optics."""
        if self.channel is None:
            return False
        self.parking = True
        try:
            wave, burst = fields(self.channel.query('BSWV?')), fields(self.channel.query('BTWV?'))
        except Exception:
            return True
        finally:
            self.parking = False
        return wave.get('WVTP') != 'DC' and burst.get('STATE') != 'ON'

    def static_at_centre(self):
        """Read-only (v4.17): True only when a readback PROVES CH2 is a static DC at the centre with burst OFF. An ARB
        carrier with burst ON (armed: an edge may have started it, 2026-10-23 03:12Z ce388df2) is NOT static, and an
        unreadable channel is not either."""
        if self.channel is None:
            return True
        self.parking = True
        try:
            return bool(self.channel.parked_dc(self.centre_text))
        except Exception:
            return False
        finally:
            self.parking = False

    def park_on_abort(self, reason):
        """v4.17 (fix of 2026-10-23 03:12Z ce388df2): on every non-normal end, park CH2 at the DC centre AT ONCE (DC
        first), before the FF unwind, unless CH2 is proven static at the centre. v4.16 parked only a continuously playing
        record (playing(): ARB with burst OFF); an EXT-armed burst that an edge had started kept playing through the
        owner's 6.4 s unwind. At the centre every FF term is ~0, so this is safe while FF is still active or unwinding."""
        if self.channel is None or self.uncertain_output:
            return False
        if self.static_at_centre():
            self.journal('awg_park_skipped', dict(stage='park_on_abort', centre=self.centre_text, reason=reason,
                                                  why='CH2 read back static DC at the centre, burst OFF'))
            return False
        return self.park_centre(reason)

    def park_centre(self, reason='FF not restored'):
        """FF NOT restored (or unknown): the safe AWG state is a static DC at the CENTRE, where the FF term
        multiplier*(ADC - baseline) is ~0 -- not the 0 V rest (that would apply the full FF offset) and not a playing
        ARB (that modulates the laser through the FF multipliers; 2026-10-22 16:39Z). The lease stays retained and the
        output stays uncertain for review; park_dc switches the output OFF only if the DC park itself does not take.
        The park bypasses the native checkpoint (keepalive, cancellation): it must run when the owner is faulted."""
        if self.channel is None:
            return False
        self.journal('awg_write_intent', dict(stage='park_centre', centre=self.centre_text, reason=reason))
        self.parking = True                       # independent of the native owner / keepalive / cancellation (v4.4)
        try:
            self.channel.park_dc(self.centre_text)
        finally:
            self.parking = False
        self.journal('awg_write_verified', dict(stage='park_centre', centre=self.centre_text))
        self.parked_centre = True
        return True

    def play(self):
        self.journal('awg_write_intent', dict(stage='play', name=self.name))
        self.uncertain_output = True
        try:
            self.channel.continuous(self.name, self.wave, self.resolved)
        except AWGStateMismatch as exc:
            self.uncertain_output = False
            self.journal('awg_state_mismatch', dict(stage='play', error=str(exc).split('; raw ')[0], raw=exc.raw))
            raise
        self.uncertain_output = False
        self.journal('awg_write_verified', dict(stage='play', timing='host_observed_continuous'))

    def arm(self, source='EXT', cycles=1):
        """One trigger plays the whole record once (cycles=1) or exactly ``cycles`` times (v4.19 continuous: the record
        repeats on the AWG clock, started by one MTRIG). source EXT = external rising edge, MAN = software (fire())."""
        self.journal('awg_write_intent', dict(stage='arm', name=self.name, source=source, cycles=cycles))
        self.uncertain_output = True
        metadata = dict(duration_s=self.wave['duration_s'], sample_rate_hz=self.wave['sample_rate_hz'])
        try:
            armed = self.channel.arm(self.name, metadata, self.resolved, source=source,
                                     **(dict(cycles=cycles) if cycles != 1 else {})) or {}
        except AWGStateMismatch as exc:
            self.uncertain_output = False          # known (read back) state: the verified restore may run
            self.journal('awg_state_mismatch', dict(stage='arm', source=source, error=str(exc).split('; raw ')[0], raw=exc.raw))
            raise
        self.uncertain_output = False
        self.burst_delay_s = armed.get('burst_delay_s')
        self.journal('awg_write_verified', dict(stage='arm', trigger='software' if source == 'MAN' else 'external_rising',
                                                burst_delay_s=self.burst_delay_s, cycles=cycles,
                                                one_edge_starts_entire_record=True))

    def fire(self):
        """Software trigger (BTWV MTRIG). A lost reply leaves the output uncertain: never re-fired, AWG retained for review."""
        # Check BEFORE marking the output uncertain: a refusal here (cancel, lease or FF-session loss) happens before
        # any write, so the verified restore may still run (2026-10-22 18:59Z R4 stopped at this point and was left
        # 'uncertain' although MTRIG was never sent).
        self._checkpoint()
        self.journal('awg_write_intent', dict(stage='fire', name=self.name))
        self.uncertain_output = True
        ack = self.channel.fire()
        self.uncertain_output = False
        self.journal('awg_write_verified', dict(stage='fire', ack=ack))
        return ack

    def close(self):
        """Call only after independently verified FF restoration."""
        if self.uncertain_output:
            return False
        if self.channel is None:
            return True
        self.cleaning = True  # Cancellation cannot skip independently safe restoration.
        if self.changed or self.rest_differs():
            # v4.17: the restore target is the CONFIGURED rest level (native_feedforward_awg.rest_v) when it is set:
            # DC first (park_dc: WVTP DC, burst OFF, OFST), then the output state compared with the pre-shot snapshot
            # (ON, load, polarity). Without rest_v: the pre-shot snapshot as before. 2026-10-23 03:12Z ce388df2 restored
            # the snapshot DC 0 V (rest_v 0.0) -> ADC ~25 counts, which looked like CH2 OFF.
            target = self.rest_state()
            self.journal('awg_write_intent', dict(stage='restore', rest_v=self.config.get('rest_v'),
                                                  target_offset=target['wave']['OFST']))
            self.uncertain_output = True
            self.channel.restore(target)
            back = fields(self.channel.query('BSWV?'))
            burst = fields(self.channel.query('BTWV?'))
            output = str(self.channel.query('OUTP?'))
            if (back.get('WVTP') != 'DC' or burst.get('STATE') != 'OFF'
                    or abs(number(back.get('OFST', 'nan')) - number(target['wave']['OFST'])) > self.config['centre_tolerance_v']
                    or output.split(' ', 1)[-1].replace(' ', '').split(',')[0] != 'ON'):
                raise RuntimeError('AWG rest readback mismatch: %s %s %s' % (back, burst, output))
            self.changed = False
            self.uncertain_output = False
            self.journal('awg_write_verified', dict(stage='restore', rest=dict(WVTP='DC', OFST=back.get('OFST'),
                                                                                  burst='OFF', output='ON')))
        self.channel.io.close()
        self.channel = None
        return True

    def rest_state(self):
        """The verified rest the restore returns CH2 to: the snapshot with OFST = rest_v when rest_v is configured."""
        state = copy.deepcopy(self.before)
        rest_v = self.config.get('rest_v')
        if rest_v is not None:
            state['wave']['OFST'] = f'{float(rest_v):.6g}'
        return state

    def rest_differs(self):
        rest_v = self.config.get('rest_v')
        return bool(self.before) and rest_v is not None and abs(number(self.before['wave']['OFST']) - float(rest_v)) > self.config['centre_tolerance_v']

    def review_details(self):
        return dict(uncertain_output=self.uncertain_output, parked_centre=getattr(self, 'parked_centre', False),
                    lease=getattr(getattr(self.channel, 'io', None), 'lease', None),
                    changed=self.changed)


class LeaseKeepalive:
    """Service the same native lease while AWG RPCs block the worker thread.

    This thread only performs liveness calls. It never clears uncertain output
    or issues physical commands, and is joined before the session returns.
    """

    def __init__(self, port):
        self.port, self.stop = port, threading.Event()
        heartbeat = port.policy['heartbeat_timeout_s']
        self.interval = min(1., heartbeat / 4, port.ff_profile['heartbeat_timeout_s'] / 4)
        self.error = None
        self.ff_lost = None
        self.thread = threading.Thread(target=self._run, name='native-ff-keepalive', daemon=True)

    def _beat(self):
        self.port.heartbeat()
        token = getattr(self.port, 'ff_token', None)
        if not self.stop.is_set() and token is not None and self.ff_lost is None:
            from ldcon.owners.esp32_native_client import FFTokenLost
            try:
                import inspect
                if 'token' in inspect.signature(self.port.ff_heartbeat).parameters:
                    state = self.port.ff_heartbeat(token=token)
                else:
                    state = self.port.ff_heartbeat()
                if isinstance(state, dict) and state.get('state') in ('unwinding', 'restored', 'fault'):
                    # the owner ended this FF session on its own: the session must not go on (fire) as if FF ran
                    self.ff_lost = 'owner ended FF (%s): %s' % (state.get('state'), state.get('reason'))
            except FFTokenLost as exc:
                if getattr(self.port, 'ff_token', None) != token:
                    return          # raced with ff_begin replacing the token: benign (2026-10-22 18:59Z R4)
                self.ff_lost = str(exc)     # the owner no longer runs this FF session; the LEASE keepalive goes on

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                self._beat()
            except MeasurementUnavailable:
                pass  # Owner's finite heartbeat/missing watchdog remains authoritative.
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self):
        self._beat()
        self.thread.start()
        return self

    def check(self, lease_only=False):
        """Lease failure: always raises. FF token loss (the owner ended or replaced this FF session): raises for the
        session's own checkpoints, but not for the AWG cleanup (lease_only), which must still restore the AWG."""
        if self.error is not None:
            raise RuntimeError('Native lease keepalive failed: ' + str(self.error))
        if self.ff_lost is not None and not lease_only:
            raise RuntimeError('Native FF session no longer owned by this session: ' + self.ff_lost)

    def lease_check(self):
        return self.check(lease_only=True)

    def __exit__(self, *_):
        self.stop.set()
        # Each liveness RPC has the existing port's finite network timeout;
        # check stop before a second RPC so the join covers at most one call.
        self.thread.join(timeout=getattr(self.port, 'timeout_s', 30.) + 1.)
        if self.thread.is_alive():
            raise RuntimeError('Native keepalive RPC exceeded its transport deadline; review required')


class NativeFFSweepSession:
    def __init__(self, port, awg, plan, *, clock=time.monotonic, sleep=time.sleep,
                 cancel=lambda: False, emit=lambda *_: None, sample_interval_s=.05):
        if not .01 <= sample_interval_s <= .5:
            raise ValueError('Invalid FF supervision interval')
        self.port, self.awg, self.plan = port, awg, plan
        self.clock, self.sleep, self.cancel, self.emit = clock, sleep, cancel, emit
        self.sample_interval_s = sample_interval_s

    def run(self):
        if hasattr(self.port, 'ff_token'):
            self.port.ff_token = None       # never heartbeat a previous session's token (v4.8)
        guardian = LeaseKeepalive(self.port)
        guardian.__enter__()
        self.guardian = guardian
        self.awg.set_checkpoint(self._check_cancel, guardian.lease_check)
        result = None
        try:
            result = self._run()
            try:
                guardian.check(lease_only=True)     # after the restore an ended FF session is expected (v4.8)
            except RuntimeError as exc:
                result['status'] = 'review_required'
                result['errors'].append(str(exc))
        finally:
            try:
                guardian.__exit__()
            except Exception as exc:
                if result is None:
                    raise
                result['status'] = 'review_required'
                result['errors'].append('Keepalive cleanup: ' + str(exc))
        # One definitive outcome, only after restoration and keepalive teardown.
        self.emit('ff_session_result', result)
        return result

    MEASUREMENT_RETRY_S = 30.

    def retry_measurement(self, label, fn, *args):
        """Owner measurement-class refusals (BrokerError code 'measurement' -> MeasurementUnavailable, e.g. "Three
        fresh independent acquisitions are required" right after the centre write) are refusals BEFORE any mutation:
        wait and retry within MEASUREMENT_RETRY_S (2026-10-22 R1 finding: the session aborted on the first one). Never
        after an uncertain native RPC; cancellation and the lease keepalive are checked between attempts."""
        from ldcon.core.contracts import MeasurementUnavailable
        started = self.clock()
        attempts = 0
        while True:
            try:
                return fn(*args)
            except MeasurementUnavailable as exc:
                attempts += 1
                if getattr(self.port, 'uncertain_output', False):
                    raise
                waited = self.clock() - started
                if waited >= self.MEASUREMENT_RETRY_S:
                    raise RuntimeError('%s: measurement refusals persisted %.0f s (%d attempts): %s' % (label, waited, attempts, exc)) from exc
                self.emit('ff_measurement_wait', dict(stage=label, attempt=attempts, waited_s=round(waited, 3), reason=str(exc)))
                self._check_cancel()
                self.sleep(max(.2, self.sample_interval_s))

    def _run(self):
        result = dict(status='failed', ff_restored=False, awg_restored=False,
                      playback_phase_verified=False, errors=[])
        begun = prepared = False
        profile = self.port.ff_profile
        # Owner rejection precedes even AWG lease acquisition.
        validated = self.retry_measurement('ff_validate', self.port.ff_validate, self.plan)
        if not validated['ok']:
            return dict(result, status='rejected', errors=validated['reasons'])
        wave = compile_wave(self.plan, profile['frequency_hz_per_v'])
        try:
            self._check_cancel()
            self.awg.prepare(wave, profile['awg_center_v'])
            prepared = True
            self._check_cancel()
            self.awg.stage()
            self._check_cancel()
            self.retry_measurement('ff_begin', self.port.ff_begin, self.plan)
            begun = True
            self._check_cancel()
            self.awg.play()
            self._monitor(wave['duration_s'])
            result.update(status='succeeded', cycles_requested=wave['cycles'],
                          observation_duration_s=wave['duration_s'],
                          timing_basis='Host-timed observation of continuous ARB; exact playback phase unverified')
        except InterruptedError as exc:
            result['status'] = 'cancelled'
            result['errors'].append(str(exc))
        except Exception as exc:
            result['errors'].append(str(exc))
        finally:
            # Restoration has independent paths. An AWG exception cannot skip
            # FF cleanup; an uncertain FF mutation must never be replayed.
            ff_uncertain = getattr(self.port, 'uncertain_output', False)
            self.park_if_playing(result, begun, prepared)
            if begun and not ff_uncertain:
                try:
                    self.port.ff_end(reason='sweep session finished')
                    deadline = self.clock() + self.port.unwind_timeout_s
                    while True:
                        try:
                            self.port.heartbeat()
                            state = self.port.ff_status()
                        except MeasurementUnavailable:
                            if self.clock() >= deadline:
                                raise RuntimeError('FF restoration status remained unavailable until its deadline')
                            self.sleep(self.sample_interval_s)
                            continue
                        self.emit('ff_restore', state)
                        if state['state'] == 'restored':
                            result['ff_restored'] = True
                            break
                        if state['state'] == 'fault' or self.clock() >= deadline:
                            raise RuntimeError('FF restoration was not verified within its deadline')
                        self.sleep(self.sample_interval_s)
                except Exception as exc:
                    result['errors'].append('FF cleanup: ' + str(exc))
            elif not begun and not ff_uncertain:
                result['ff_restored'] = True
            if prepared and result['ff_restored']:
                try:
                    result['awg_restored'] = self.awg.close()
                except Exception as exc:
                    result['errors'].append('AWG cleanup: ' + str(exc))
                    # v4.4: a failed restore (e.g. the native keepalive failed) must not leave CH2 armed or playing: park at
                    # the DC centre (SDG-only, independent of the native owner); leases retained for review.
                    try:
                        if self.awg.park_centre():
                            result['errors'].append('AWG parked at the DC centre after the failed restore (review)')
                    except Exception as exc2:
                        result['errors'].append('AWG park at the centre: ' + str(exc2))
            elif prepared:
                try:
                    if self.awg.park_centre():
                        result['errors'].append('AWG parked at the DC centre (FF not restored; leases retained for review)')
                except Exception as exc:
                    result['errors'].append('AWG park at the centre: ' + str(exc))
            if not result['ff_restored'] or (prepared and not result['awg_restored']):
                result['status'] = 'review_required'
            result['awg_review'] = self.awg.review_details()
        return result

    def park_if_playing(self, result, begun, prepared):
        """FF enabled and CH2 playing a record continuously (continuous mode, or an arming that did not take):
        park CH2 at the DC CENTRE BEFORE the FF unwind. At the centre the FF term is ~0 and the laser is no longer
        modulated, so the owner's unwind (which waits for agreeing independent optics) can complete; 2026-10-22 16:39Z
        the record kept playing through the whole unwind and the owner faulted at its deadline. This is a move to a
        verified static state (read back), never a replay of an uncertain command."""
        if not prepared:
            return
        if result.get('status') != 'succeeded' or result.get('abort') or result.get('park_first'):
            # v4.17: a non-normal end (error, owner ended FF, cancel, READY failure) parks at once unless CH2 is proven
            # static at the centre (an armed burst may have been started by an edge: 2026-10-23 03:12Z ce388df2)
            reason = '; '.join(str(e) for e in result.get('errors') or []) or str(result.get('status'))
            try:
                if self.awg.park_on_abort(reason[:200]):
                    result['parked_on_abort'] = True
                    if not result.get('park_first'):
                        result['errors'].append('AWG CH2 parked at the DC centre at once (abort, %s FF unwind)'
                                                % ('before the' if begun else 'no'))
            except Exception as exc:
                result['errors'].append('AWG park at the centre on abort: ' + str(exc))
            return
        try:
            if self.awg.playing() and self.awg.park_centre():
                result['errors'].append('AWG record was playing%s: parked at the DC centre before the FF unwind'
                                        % (' with FF enabled' if begun else ''))
        except Exception as exc:
            result['errors'].append('AWG park at the centre before the FF unwind: ' + str(exc))

    def _check_cancel(self):
        self.guardian.check()
        if self.cancel():
            raise InterruptedError('FF sweep cancelled; bounded restoration requested')

    def _monitor(self, duration):
        deadline = self.clock() + duration
        missing_since = None
        while self.clock() < deadline:
            self._check_cancel()
            try:
                self.port.heartbeat()
                self.port.ff_heartbeat()
                state = self.port.ff_status()
            except MeasurementUnavailable:
                if missing_since is None:
                    missing_since = self.clock()
                if self.clock() - missing_since >= self.port.ff_profile['missing_timeout_s']:
                    raise RuntimeError('FF status unavailable beyond the approved missing deadline')
                self.sleep(min(self.sample_interval_s, max(0., deadline - self.clock())))
                continue
            missing_since = None
            if state['state'] != 'active':
                raise RuntimeError('Owner ended FF sweep: ' + str(state.get('reason')))
            self.emit('ff_trace', state)
            self.sleep(min(self.sample_interval_s, max(0., deadline - self.clock())))
        if missing_since is not None:
            raise RuntimeError('FF observation ended without a fresh terminal status')


class RequestJournal:
    """Durable at-most-once attachment records, scoped to one controller job.

    Submission/claim are serialized by a local advisory request lock.
    A crash after claimed is review_required; no output request is replayed.
    """

    def __init__(self, directory, job_id, revision):
        self.directory, self.job_id, self.revision = Path(directory), job_id, revision
        self.directory.mkdir(parents=True, exist_ok=True)

    def _save(self, path, value):
        tmp = path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        with (self.directory / '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def claim(self, request, *, now):
        with self._locked():
            return self._claim(request, now=now)

    def _claim(self, request, *, now):
        identifier = request.get('request_id')
        if (not isinstance(identifier, str) or len(identifier) != 32
                or any(c not in '0123456789abcdef' for c in identifier)):
            raise ValueError('Invalid FF request ID')
        path = self.directory / (identifier + '.json')
        if path.exists():
            old = json.loads(path.read_text())
            if old['status'] == 'claimed':
                self._save(path, dict(old, status='review_required', reason='Previous claim outcome unknown'))
            raise ValueError('FF request has already been claimed; never replay')
        if (request.get('job_id') != self.job_id or request.get('settings_revision') != self.revision
                or type(request.get('expires_unix_s')) not in (int, float)
                or not math.isfinite(request['expires_unix_s']) or now >= request['expires_unix_s']):
            raise ValueError('Expired or mismatched FF request')
        self._save(path, dict(request, status='claimed'))
        return path

    def finish(self, path, result):
        with self._locked():
            path = Path(path)
            if path.parent.resolve() != self.directory.resolve():
                raise ValueError('FF journal result must belong to this controller')
            old = json.loads(path.read_text())
            if old['status'] != 'claimed':
                raise ValueError('FF request is not awaiting its first outcome')
            self._save(path, dict(old, status=result['status'], result=result))


class NativeFFAttachment:
    """Controller-worker hook; reception and cancellation use its existing gate.

    This preparation does not register a Web start route or enable any deck.
    The caller must validate the registered card/job/revision before receive.
    Every hardware admission is still revalidated by the native owner.
    """

    def __init__(self, receive, journal, awg_factory, *, clock=time.monotonic,
                 wall_clock=time.time, sleep=time.sleep, cancel=lambda: False,
                 emit=lambda *_: None):
        self.receive, self.journal, self.awg_factory = receive, journal, awg_factory
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.cancel, self.emit = cancel, emit

    def __call__(self, port, *, online_busy=False):
        request = self.receive()
        if request is None:
            return None
        path = self.journal.claim(request, now=self.wall_clock())
        if online_busy:
            result = dict(status='rejected', errors=['An online PID comparison is active'])
        elif not port.active:
            result = dict(status='rejected', errors=['An existing active native lock is required'])
        else:
            # No connect()/claim() on the native port; same worker and lease.
            try:
                result = NativeFFSweepSession(port, self.awg_factory(), request['plan'],
                    clock=self.clock, sleep=self.sleep, cancel=self.cancel, emit=self.emit).run()
            except Exception as exc:
                # A claimed intent with no definitive session outcome is not
                # an ordinary rejected request and cannot be sent again.
                result = dict(status='review_required', errors=[str(exc)])
        self.journal.finish(path, result)
        return result
