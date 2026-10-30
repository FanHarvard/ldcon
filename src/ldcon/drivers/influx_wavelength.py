"""Read new ESP32-consumed wavelengths; no averaging, device calls or DB writes."""
import json
import math
import time

from ldcon.jobs import shot_analysis as storage

FIELDS = ('system_value', 'system_seq', 'is_new_system_value', 'system_latency_us', 'dac_val')


class InfluxWavelengthHistory:
    def __init__(self, config, query=None):
        self.config = config
        self.query = query or storage._influx_query

    def read(self, start_unix_s, end_unix_s):
        if not (math.isfinite(start_unix_s) and math.isfinite(end_unix_s)
                and 0 < end_unix_s - start_unix_s <= 3600):
            raise ValueError('History window must be positive and at most one hour')
        c = self.config['influx']
        # JSON string quoting also escapes Flux string literals; never interpolate
        # user text as query code. Configuration supplies the physical host binding.
        q = ('from(bucket: %s) |> range(start: %s, stop: %s) '
             '|> filter(fn: (r) => r._measurement == %s and r.host == %s and (%s)) '
             '|> keep(columns: ["_time", "_field", "_value"])') % (
                 json.dumps(c['bucket']), storage._iso(start_unix_s), storage._iso(end_unix_s),
                 json.dumps(c['measurement']), json.dumps(self.config['esp32_host']),
                 ' or '.join('r._field == ' + json.dumps(f) for f in FIELDS))
        began = time.monotonic()
        series = storage._influx_series(self.query(self.config, q, timeout=5))
        samples, seen, faults = [], {}, []
        last_seq = None
        for stamp, nm in sorted(series.get('system_value', {}).items()):
            if series.get('is_new_system_value', {}).get(stamp) != 1:
                continue
            seq = series.get('system_seq', {}).get(stamp)
            if (not math.isfinite(nm) or nm <= 0 or seq is None or not math.isfinite(seq)
                    or seq < 0 or int(seq) != seq):
                faults.append('invalid_native_sample')
                continue
            rx = storage._parse_ns(stamp)
            if not start_unix_s <= rx < end_unix_s:
                faults.append('record_outside_query_window')
                continue
            seq = int(seq)
            if seq in seen:
                if seen[seq] != nm:
                    faults.append('conflicting_sequence')
                continue
            if last_seq is not None and seq < last_seq:
                faults.append('sequence_regression')
            seen[seq], last_seq = nm, seq
            latency = series.get('system_latency_us', {}).get(stamp)
            valid_latency = latency is not None and math.isfinite(latency) and 0 <= latency < 100000
            dac = series.get('dac_val', {}).get(stamp)
            samples.append(dict(received_unix_s=rx,
                time_unix_s=rx - latency * 1e-6 if valid_latency else rx,
                wavelength_nm=nm, system_seq=seq,
                latency_corrected=valid_latency, system_latency_us=latency if valid_latency else None,
                dac_count=dac if dac is not None and math.isfinite(dac) else None))
            if len(samples) > 200000:
                raise ValueError('History exceeds bounded study sample capacity')
        if any(b['time_unix_s'] <= a['time_unix_s'] for a, b in zip(samples, samples[1:])):
            faults.append('nonmonotonic_source_time')
        return dict(samples=samples, integrity_errors=sorted(set(faults)), query_s=time.monotonic()-began,
            provenance=dict(driver='influx_native_wavelength', quantity='optical.wavelength', unit='nm',
                measurement=c['measurement'], host=self.config['esp32_host'],
                timestamp_basis='ESP32 record time minus valid system_latency_us; otherwise record time',
                host_clock_alignment='not independently verified', aggregation='none',
                filter='is_new_system_value == 1; unique system_seq'))
