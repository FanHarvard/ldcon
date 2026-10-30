"""Read-only VictoriaMetrics storage binding for native DAC acquisition traces.

This adapter owns the legacy series names, export query and storage timestamps.
The owner retains optical qualification, fit validity and control decisions.
"""
import json
from urllib.parse import urlencode,urlsplit
from urllib.request import build_opener,ProxyHandler

from ldcon.core.contracts import DeviceError,MeasurementUnavailable


def read_native_dither(config,end,fit_window):
    endpoint=config.get('telemetry_endpoint');selector=config.get('selector')
    if not isinstance(endpoint,str) or not endpoint or not isinstance(selector,str) or not selector:
        raise DeviceError('Passive dither telemetry source is unset')
    endpoint=endpoint.rstrip('/')
    parsed=urlsplit(endpoint)
    if parsed.scheme!='http' or parsed.query or parsed.fragment:raise DeviceError('Invalid telemetry endpoint')
    # Extra history tolerates ingestion jitter without assigning fresh times.
    start=end-fit_window-5.
    query=urlencode([('match[]',selector),('start',str(start)),('end',str(end))])
    try:
        with build_opener(ProxyHandler({})).open(endpoint+'/api/v1/export?'+query,timeout=10) as response:
            raw=response.read(2*1024*1024+1)
    except OSError as error:
        raise MeasurementUnavailable('Native telemetry read failed: '+str(error)) from error
    if len(raw)>2*1024*1024:raise DeviceError('Native telemetry exceeds size limit')
    channels={}
    for line in raw.splitlines():
        try:
            item=json.loads(line);name=item['metric']['__name__'].removeprefix('ESP32ServoController_')
            if name not in ('dac_val','is_new_system_value'):continue
            if len(item['timestamps'])!=len(item['values']):raise ValueError('Mismatched telemetry arrays')
            values=dict(zip(item['timestamps'],item['values']))
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise MeasurementUnavailable('Malformed native dither telemetry') from error
        if name in channels:raise DeviceError('Ambiguous native telemetry series')
        channels[name]=values
    if set(channels)!={'dac_val','is_new_system_value'}:
        raise MeasurementUnavailable('Native dither telemetry is unavailable')
    stamps=sorted(t for t in channels['dac_val'] if channels['is_new_system_value'].get(t,0)>.5)
    if not stamps or stamps[-1]/1000<end-5.:
        raise MeasurementUnavailable('Native dither telemetry is stale')
    cutoff=stamps[-1]-fit_window*1000
    stamps=[t for t in stamps if t>=cutoff]
    return dict(times_s=[t/1000 for t in stamps],dac_counts=[channels['dac_val'][t] for t in stamps],
                provenance=dict(driver='native_dither.victoriametrics',endpoint=endpoint,selector=selector,
                    time_basis='acquisition',timestamp_unit='ms',value_unit='count',
                    start_unix_s=start,end_unix_s=end))
