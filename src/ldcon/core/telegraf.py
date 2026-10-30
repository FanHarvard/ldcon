"""Best-effort Telegraf export: measurement ldcon, shared card context."""

import math
import re
import socket
import time
from urllib.parse import urlsplit


def metric_tags(tags):
    """Execution identifiers belong in durable logs, not time-series labels."""
    execution_keys={'job','job_id','run','run_id','trial_id','request_id'}
    identifier=re.compile(r'(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})',re.I)
    return {k:v for k,v in tags.items() if k not in execution_keys
            and not identifier.fullmatch(str(v))}


def escape(value, equals=True):
    text = str(value)
    if any(c in text for c in "\r\n\\"):
        raise ValueError("Line protocol identifiers cannot contain newlines/backslashes")
    text = text.replace(" ", "\\ ").replace(",", "\\,")
    return text.replace("=", "\\=") if equals else text


def field(value):
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (float, int) and math.isfinite(value):
        return repr(float(value))
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    return None


class TelegrafExporter:
    """Best-effort UDP: SQLite is authoritative; send success is not delivery.

    Raw patterns stay in SQLite. Only scalar fields and summary events are
    exported, with bounded datagram size. A full socket buffer drops export
    rather than blocking the control path. DNS is resolved once at creation.
    """

    def __init__(self, url, laser, bucket="laserlab", simulation=True, transport=None, *, tags=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "udp" or not parsed.hostname or not parsed.port
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Expected udp://host:port")
        self.tags = (f"tlg_bucket={escape(bucket)},laser={escape(laser)},source=ldcon,"
                     f"simulation={'true' if simulation else 'false'}")
        self.context_tags=dict(tags or {})
        self.sent = self.dropped = 0
        self.last_timestamp = 0
        if transport is None:
            family, kind, protocol, _, self.address = socket.getaddrinfo(
                parsed.hostname, parsed.port, type=socket.SOCK_DGRAM,
            )[0]
            self.socket = socket.socket(family, kind, protocol)
            self.socket.setblocking(False)
        else:
            self.address = (parsed.hostname, parsed.port)
            self.socket = transport

    def send(self, run_id, simulated_s, kind, payload):
        try:
            # Scalar nested mappings become stable dotted field paths. Arrays,
            # including raw fringes, stay exclusively in durable local logs.
            values = {"simulated_s":simulated_s,"event":kind}
            def flatten(mapping,prefix="",depth=0):
                if depth>6:return
                for key,value in mapping.items():
                    if key.startswith('_'):continue
                    name=prefix+key
                    # Physical provenance text stays in durable logs/settings;
                    # numeric time-series backends otherwise turn strings into 0.
                    if name.startswith('physical.') and isinstance(value,str):continue
                    if type(value) in (str,float,int,bool):values[name]=value
                    elif isinstance(value,dict):flatten(value,name+".",depth+1)
            flatten(payload)
            custom=payload.get('_telemetry',{})
            for key,value in custom.get('fields',{}).items():
                if key not in ('run_id','simulated_s','event'):values[key]=value
            tags=dict(self.context_tags)
            tags.update(payload.get('_context',{}))
            for key,value in custom.get('tags',{}).items():
                if key in ('laser','laser_id','source','card','operation','deck','job','kind','simulation','tlg_bucket'):
                    raise ValueError('Reserved telemetry tag: '+key)
                tags[key]=value
            context=",".join(f"{escape(k)}={escape(v)}" for k,v in sorted(metric_tags(tags).items()) if v is not None)
            context=(","+context) if context else ""
            fields = [(escape(key), field(value)) for key, value in values.items()]
            body = ",".join(f"{key}={value}" for key, value in fields if value is not None)
            # Use ingestion UTC, never virtual time as a Unix timestamp.
            self.last_timestamp = max(time.time_ns(), self.last_timestamp + 1)
            line = f"ldcon,{self.tags}{context},kind={escape(kind)} {body} {self.last_timestamp}"
            packet = line.encode()
            if len(packet) > 60000:
                raise ValueError("Export datagram too large")
            self.socket.sendto(packet, self.address)
        except (OSError, ValueError, OverflowError, TypeError, AttributeError):
            self.dropped += 1
        else:
            self.sent += 1

    def close(self):
        self.socket.close()
