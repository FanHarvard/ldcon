"""ESP32 SNTP time-sync telemetry to Telegraf (interim, 2026-10-23; the firmware should export it later).

The esp32servocon /status reports its SNTP state (time_synced, sntp_*; sntp_last_step_us is the correction applied
at the last sync, i.e. the best offset estimate vs the NTP server) but the firmware does not send it as telemetry.
Until it does, the LDCon device owners forward these fields from the /status they ALREADY read (no extra device
request) as one InfluxDB line per interval to the Telegraf UDP listener:

    esp32_time_sync,tlg_bucket=laserlab,host=<ESP32 IP>,laser=<Name>,laser_id=<id>,device=<id>,source=ldcon,
        producer=<native-owner|rig-owner|status-file> time_synced=true,sntp_last_step_us=154.0,... <host ns>

(``tlg_bucket`` routes to the laserlab database as for the ``ldcon`` measurement; ``host`` is the ESP32 IP as on the
firmware's own ESP32ServoController_* series.) In VictoriaMetrics: esp32_time_sync_sntp_last_step_us{host=...}.

Safety: this is a report-only side channel. ``observe`` never raises, never blocks (non-blocking UDP, one datagram
< 1 KiB) and sends at most once per ``interval_s`` (default 10 s, minimum 1 s) whatever the poll rate; a failed send
is counted and dropped (no retry). The timestamp is the host time of the /status read (not the ESP32 clock).
Only an IP literal endpoint is accepted (no DNS on the control path).
"""

import ipaddress
import json
import math
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

MEASUREMENT = 'esp32_time_sync'
FIELDS = ('time_synced', 'sntp_update_delay_s', 'sntp_syncs', 'sntp_last_sync_age_s',
          'sntp_last_step_us', 'sntp_max_step_us', 'uptime_s')
DEFAULT_INTERVAL_S = 10.
MIN_INTERVAL_S = 1.
MAX_DATAGRAM = 1024
KILL_SWITCH = 'LDCON_ESP32_TIME_TELEMETRY'   # "off" disables the exporter (no restart of anything else needed)


def disabled_by_environment():
    return os.environ.get(KILL_SWITCH, '').strip().lower() in ('0', 'off', 'false', 'no', 'disabled')


def _endpoint(url):
    parsed = urlsplit(str(url))
    if (parsed.scheme != 'udp' or not parsed.hostname or not parsed.port
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('ESP32 time telemetry endpoint must be udp://<ip>:<port>')
    try:
        ipaddress.ip_address(parsed.hostname)   # IP literal only: no DNS lookups
    except ValueError:
        raise ValueError('ESP32 time telemetry endpoint must be udp://<ip>:<port> (IP literal, no DNS)') from None
    return parsed.hostname, parsed.port


def _tag(value):
    text = str(value)
    if not text or any(c in text for c in '\r\n\\"'):
        raise ValueError('Invalid telemetry tag value: %r' % (value,))
    return text.replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')


def _field(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float) and math.isfinite(value):
        return repr(float(value))
    return None   # strings / null / non-finite are not exported


def status_line(status, tags, timestamp_ns):
    """One line-protocol line from a /status mapping, or None if it carries none of the SNTP fields."""
    if not isinstance(status, dict):
        return None
    body = []
    for key in FIELDS:
        text = _field(status.get(key))
        if text is not None:
            body.append(key + '=' + text)
    if not body:
        return None   # older firmware without SNTP fields: nothing to report
    return '%s,%s %s %d' % (MEASUREMENT, tags, ','.join(body), int(timestamp_ns))


class TimeSyncExporter:
    def __init__(self, endpoint, *, host, laser, laser_id, device, producer, interval_s=DEFAULT_INTERVAL_S,
                 bucket='laserlab', transport=None, clock=time.monotonic, wall_ns=time.time_ns):
        self.address = _endpoint(endpoint)
        ipaddress.ip_address(str(host))   # the ESP32 IP, as the firmware's own series use
        interval_s = float(interval_s)
        if not math.isfinite(interval_s) or interval_s < MIN_INTERVAL_S:
            raise ValueError('ESP32 time telemetry interval must be >= %g s' % MIN_INTERVAL_S)
        self.interval_s = interval_s
        tags = dict(tlg_bucket=bucket, host=host, laser=laser, laser_id=laser_id, device=device,
                    source='ldcon', producer=producer)
        self.tags = ','.join('%s=%s' % (k, _tag(v)) for k, v in sorted(tags.items()))
        self.clock, self.wall_ns = clock, wall_ns
        self.last_attempt = None
        self.sent = self.dropped = self.skipped = 0
        self.last_error = None
        self.expected_host = None   # status-file sidecar: the sample.host the file must carry
        if transport is None:
            family = socket.AF_INET6 if ':' in self.address[0] else socket.AF_INET
            transport = socket.socket(family, socket.SOCK_DGRAM)
            transport.setblocking(False)
        self.socket = transport

    def due(self):
        return self.last_attempt is None or self.clock() - self.last_attempt >= self.interval_s

    def observe(self, status, read_unix_s=None):
        """Report-only hook for every /status read. Never raises; at most one datagram per interval."""
        try:
            if not self.due():
                return False
            stamp = int(read_unix_s * 1e9) if read_unix_s is not None else self.wall_ns()
            line = status_line(status, self.tags, stamp)
            if line is None:
                self.skipped += 1
                return False
            self.last_attempt = self.clock()   # rate limit holds even when the send fails
            packet = line.encode()
            if len(packet) > MAX_DATAGRAM:
                raise ValueError('ESP32 time telemetry datagram too large')
            self.socket.sendto(packet, self.address)
        except Exception as exc:   # noqa: BLE001 -- telemetry must never reach the control path
            self.dropped += 1
            self.last_error = '%s: %s' % (type(exc).__name__, exc)
            return False
        self.sent += 1
        return True

    __call__ = observe

    def counters(self):
        return dict(sent=self.sent, dropped=self.dropped, skipped=self.skipped, last_error=self.last_error,
                    interval_s=self.interval_s)

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


def validate_spec(spec):
    """Owner-config block {telegraf_endpoint, laser, laser_id, interval_s?}; raises ValueError at startup."""
    if not isinstance(spec, dict) or set(spec) - {'telegraf_endpoint', 'laser', 'laser_id', 'interval_s', 'enabled'}:
        raise ValueError('esp32_time_telemetry must be {telegraf_endpoint, laser, laser_id, interval_s?, enabled?}')
    _endpoint(spec.get('telegraf_endpoint'))
    for key in ('laser', 'laser_id'):
        _tag(spec.get(key) or '')
    if type(spec.get('enabled', True)) is not bool:
        raise ValueError('esp32_time_telemetry.enabled must be a boolean')
    interval = spec.get('interval_s', DEFAULT_INTERVAL_S)
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval < MIN_INTERVAL_S:
        raise ValueError('esp32_time_telemetry.interval_s must be >= %g' % MIN_INTERVAL_S)
    return dict(spec)


def from_spec(spec, *, host, device, producer, log=print):
    """Exporter for an owner, or None (no spec / disabled / setup failure). Setup failure never stops the owner."""
    if spec is None or disabled_by_environment():
        return None
    spec = validate_spec(spec)
    if not spec.get('enabled', True):
        return None
    try:
        return TimeSyncExporter(spec['telegraf_endpoint'], host=host, laser=spec['laser'],
                                laser_id=spec['laser_id'], device=device, producer=producer,
                                interval_s=spec.get('interval_s', DEFAULT_INTERVAL_S))
    except (OSError, ValueError) as exc:
        log('ESP32 time telemetry disabled: %s' % exc, flush=True)
        return None


def for_laser(laser, *, host, device, producer, log=print):
    """Rig owners: the laser definition's own telemetry endpoint (``telemetry.telegraf_endpoint``)."""
    endpoint = ((laser or {}).get('telemetry') or {}).get('telegraf_endpoint')
    if not endpoint:
        return None
    try:
        return from_spec(dict(telegraf_endpoint=endpoint, laser=laser['name'], laser_id=laser['id']),
                         host=host, device=device, producer=producer, log=log)
    except (KeyError, TypeError, ValueError) as exc:
        log('ESP32 time telemetry disabled: %s' % exc, flush=True)
        return None


# ---- status-file sidecar (no device access, no owner restart) -------------------------------------------------

def sidecar_step(exporter, status_file, last_stamp, *, now=time.time, max_age_s=30.):
    """Forward the owner's last /status from its published status file once; returns the stamp used (or last)."""
    try:
        state = json.loads(Path(status_file).read_text())
        sample = state.get('sample') or {}
        stamp = state.get('last_success_unix_s')
        status = sample.get('servo_status')
        host = sample.get('host')
    except (OSError, ValueError, AttributeError):
        return last_stamp
    if (type(stamp) not in (int, float) or stamp == last_stamp or not 0 <= now() - stamp <= max_age_s
            or not isinstance(status, dict)):
        return last_stamp   # stale or unchanged owner sample: send nothing
    if exporter.expected_host is not None and host != exporter.expected_host:
        exporter.dropped += 1
        exporter.last_error = 'status file host %r is not %r' % (host, exporter.expected_host)
        return stamp
    exporter.observe(status, read_unix_s=stamp)
    return stamp


def main(argv=None):
    import argparse
    import signal
    import threading
    parser = argparse.ArgumentParser(description='Forward ESP32 SNTP status from an owner status file to Telegraf')
    parser.add_argument('--status-file', type=Path, required=True)
    parser.add_argument('--host', required=True, help='ESP32 IP (host tag; must match sample.host)')
    parser.add_argument('--device', required=True)
    parser.add_argument('--laser', required=True)
    parser.add_argument('--laser-id', required=True)
    parser.add_argument('--endpoint', default='udp://127.0.0.1:8094')
    parser.add_argument('--interval-s', type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='validate and print the line that would be sent; no send')
    args = parser.parse_args(argv)
    exporter = TimeSyncExporter(args.endpoint, host=args.host, laser=args.laser, laser_id=args.laser_id,
                                device=args.device, producer='status-file', interval_s=args.interval_s)
    exporter.expected_host = args.host
    if args.check:
        state = json.loads(args.status_file.read_text())
        sample = state.get('sample') or {}
        print(json.dumps(dict(sample_host=sample.get('host'), age_s=time.time() - (state.get('last_success_unix_s') or 0),
                              line=status_line(sample.get('servo_status'), exporter.tags, time.time_ns()))))
        return 0 if sample.get('host') == args.host else 1
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    last, reported = None, 0.
    while not stop.is_set():
        last = sidecar_step(exporter, args.status_file, last)
        if args.once:
            break
        if time.monotonic() - reported >= 3600:
            reported = time.monotonic()
            print('esp32 time telemetry: ' + json.dumps(exporter.counters()), flush=True)
        stop.wait(exporter.interval_s)
    exporter.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
