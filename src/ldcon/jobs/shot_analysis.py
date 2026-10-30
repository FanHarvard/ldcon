"""Post-shot analysis of one locked-staircase shot (contract ``ldcon.shot-analysis/v1``; design rule F, rule C).

Human approval 2026-10-23 ~04:40Z: the fine per-point analysis runs AFTER the shot, as a standalone, read-only,
one-shot procedure (card ``analyze.shot_sweep``). The sweep proxy calls the SAME procedure as its own action when it
publishes the shot's outcome: it starts a detached child process (``launch``) and never waits for it, so the analysis
can never delay the outcome, the proxy's end or the next shot. The card runs it for any proxy job / request id.

What it does (no device access; reads only InfluxDB/VictoriaMetrics, job files and the owners' adc-log history):
  1. fetch the shot window of the ESP32 full-data rows (system_value, mod_raw, is_new_system_value,
     system_latency_us, system_seq) and the threshold-logged adc_raw rows -- InfluxDB first (rule C: complete
     0.13-0.26 s after the record end), VictoriaMetrics as the fallback (searchable only ~3.5 s later; we wait);
  2. fit the programmed record shape (levels, lead/tail ramps, the native route's per-level edge, an early stop) to
     mod_raw + adc_raw -> ONE start time T0; level i starts at T0 + i*dwell;
  3. per wavemeter point (time = receive time - system_latency): level_index (-1 before the levels, N after),
     in_transition, t_since_transition_s, offset from the pre-record reference, error vs the programmed level;
  4. the level map (index <-> offset, target frequency/wavelength, t_start/t_end, played), the parameters used,
     revisions, the ESP32 clock (esp32_time_sync), the owner's temporary ADC log threshold, quality flags.
Times are ESP32 unix time (SNTP). Prototype and validation: preparations/post-shot-analysis-20261023/.

Mode quality (v3.16/v4.25, human 2026-10-23 ~12:10Z): where the lock-side session logged owner samples with the optical
mode metrics (sr422 rig: the lock job's ``sweep_sample`` events), every wavemeter point gets the nearest owner sample's
quality columns (``quality_single_mode``, ``quality_degraded``, ``quality_side_peak_height_max``,
``quality_side_peak_prominence_max``, ``quality_main_fwhm_fraction``, ``quality_match_dt_s``), and ``mode_quality`` holds
the owner samples themselves (located by level index / commanded offset) and the degradation summary (sweep_tracking).
"""
import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

CONTRACT = 'ldcon.shot-analysis/v1'
C = 299792458.0
DIRNAME = 'shot-analysis'
CONFIG_NAME = 'shot-analysis.json'
FULL_FIELDS = ('system_value', 'mod_raw', 'is_new_system_value', 'system_latency_us', 'system_seq')
CLOCK_STEP_WARN_US = 500.
TIME_FIELDS = ('time_synced', 'sntp_update_delay_s', 'sntp_syncs', 'sntp_last_sync_age_s', 'sntp_last_step_us',
               'sntp_max_step_us', 'uptime_s')
DEFAULTS = dict(influx=dict(url='http://127.0.0.1:8181', org='fan.group.harvard', bucket='laserlab',
                            measurement='ESP32ServoController', token_file='/etc/telegraf/telegraf.conf'),
                vm=dict(url='http://127.0.0.1:8428', db='laserlab', force_flush=False),
                sources=['influx', 'vm'], influx_wait_s=0.5, vm_wait_s=3.5, complete_margin_s=0.5,
                pre_s=3.0, post_s=1.5, max_runtime_s=45.0, prior_delay_s=0.025, settle_s=None,
                owner_adc_log_history=None, esp32_host=None)


# ---------------------------------------------------------------------------------------------------- files
def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    with tmp.open('w') as stream:
        json.dump(value, stream, allow_nan=False, default=_jsonable)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _jsonable(value):
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    raise TypeError('not JSON serialisable: %r' % type(value))


def clean(value):
    """NaN/inf -> None, numpy -> python, recursively (the contract is strict JSON)."""
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
    except ImportError:
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def analysis_version():
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def state_root_of(job_dir):
    """var/deployments/<laser> for a job directory var/deployments/<laser>/jobs/<set>/<id> (the nearest ancestor that
    holds web-server.json or shot-analysis.json; else three levels up)."""
    job_dir = Path(job_dir)
    for parent in list(job_dir.parents)[:4]:
        if (parent / CONFIG_NAME).exists() or (parent / 'web-server.json').exists():
            return parent
    return job_dir.parent.parent.parent


def load_config(state_root):
    """<state root>/shot-analysis.json (installed by the deploy script; no secrets in it, the Influx token is read from
    ``influx.token_file``). None when absent: the analysis is then 'unavailable' (not configured)."""
    raw = _read_json(Path(state_root) / CONFIG_NAME)
    if not isinstance(raw, dict):
        return None
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in raw.items():
        if key in ('influx', 'vm') and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    if not isinstance(cfg.get('esp32_host'), str) or not re.fullmatch(r'[0-9.]+', cfg['esp32_host']):
        raise ValueError('shot-analysis.json: esp32_host must be the ESP32 IP literal (the telemetry host tag)')
    return cfg


def analysis_dir(proxy_dir):
    return Path(proxy_dir) / DIRNAME


def read_status(proxy_dir):
    return _read_json(analysis_dir(proxy_dir) / 'status.json')


def read_result(proxy_dir):
    return _read_json(analysis_dir(proxy_dir) / 'result.json')


def write_status(proxy_dir, **value):
    current = read_status(proxy_dir) or {}
    current.update(value, updated_unix_s=time.time())
    _atomic_json(analysis_dir(proxy_dir) / 'status.json', clean(current))
    return current


def ref_for(proxy_job):
    return 'jobs/%s/analysis' % proxy_job


# ----------------------------------------------------------------------------------------------- launching
def block(state, proxy_job=None, reason=None, **extra):
    """outcome.analysis = {state: pending|ready|unavailable, ref, reason, ...} (sweep API v1.4 hook, v1.5 filled)."""
    return dict(state=state, ref=ref_for(proxy_job) if proxy_job and state != 'unavailable' else None,
                reason=reason, schema=CONTRACT, **extra)


LAUNCHER = None      # tests replace this with a callable(argv, log_path) -> None
# 2026-10-23 06:37-06:47Z (every proxy-launched analysis stuck 'running'): the jobs service runs with
# PYTHONNOUSERSITE=1, i.e. the SYSTEM numpy 1.26.4 (FlexiBLAS -> OpenMP OpenBLAS, 12 threads). Under the child's own
# 2 GiB address-space limit OpenBLAS cannot allocate its per-thread buffers and spins in blas_memory_alloc (gdb: lstsq ->
# dgelsd -> dgemm -> blas_memory_alloc -> malloc, R state, VmSize ~2.0 GB) until RLIMIT_CPU sends SIGXCPU: a silent
# death, status left 'running'. A hand run (env -i drops PYTHONNOUSERSITE -> user-site numpy 2.5.1) was ready in 1 s.
# The analysis is small linear algebra: one BLAS thread, a larger address-space bound, and SIGXCPU/faults reported.
CHILD_ENV = {'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'FLEXIBLAS_NUM_THREADS': '1'}
CHILD_AS_BYTES = 8 << 30


def launch(proxy_dir, *, request=None, outcome=None):
    """Start the analysis of this proxy's shot in a DETACHED child (never waited for) and return outcome.analysis.
    Never raises: any problem gives {state: 'unavailable', reason}. ``request`` carries what the proxy knows (plan,
    target_hz, lock_dir, trigger, record_s, programmed); written to shot-analysis/request.json for the child."""
    proxy_dir = Path(proxy_dir)
    job = proxy_dir.name
    try:
        if os.environ.get('LDCON_SHOT_ANALYSIS', '').lower() in ('off', '0', 'false'):
            return block('unavailable', job, 'post-shot analysis disabled (LDCON_SHOT_ANALYSIS=off)')
        outcome = outcome or {}
        if (request or {}).get('calibrate_only'):
            return block('unavailable', job, 'calibration request: no record is played')
        if outcome.get('playback_started_unix_s') is None:
            return block('unavailable', job, 'no playback was detected; nothing to analyse')
        root = state_root_of(proxy_dir)
        cfg = load_config(root)
        if cfg is None:
            return block('unavailable', job, 'post-shot analysis is not configured for this laser (%s missing)' % CONFIG_NAME)
        deadline = time.time() + float(cfg['max_runtime_s']) + float(cfg['vm_wait_s']) + 10.
        _atomic_json(analysis_dir(proxy_dir) / 'request.json',
                     clean(dict(request or {}, proxy_job=job, outcome=outcome, requested_unix_s=time.time())))
        write_status(proxy_dir, schema=CONTRACT, state='pending', ref=ref_for(job), reason=None,
                     requested_unix_s=time.time(), deadline_unix_s=deadline, attempt_by='proxy', pid=None)
        argv = [sys.executable, '-P', '-m', 'ldcon.jobs.shot_analysis', 'run', '--proxy-dir', str(proxy_dir)]   # -P: no cwd on sys.path
        log_path = analysis_dir(proxy_dir) / 'analysis.log'
        if LAUNCHER is not None:
            LAUNCHER(argv, log_path)
        else:
            with open(log_path, 'ab') as log:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                         start_new_session=True, cwd=str(proxy_dir), env=dict(os.environ, **CHILD_ENV))
            write_status(proxy_dir, pid=child.pid)
        return block('pending', job, None, deadline_unix_s=deadline)
    except Exception as error:       # noqa: BLE001 - the outcome must never fail because of the analysis
        try:
            write_status(proxy_dir, schema=CONTRACT, state='unavailable', ref=None, reason='launch failed: %s' % error)
        except Exception:            # noqa: BLE001
            pass
        return block('unavailable', job, 'post-shot analysis could not be started: %s' % str(error)[:300])


# ------------------------------------------------------------------------------------------------- inputs
def _utc_seconds(stamp):
    from datetime import datetime
    return datetime.fromisoformat(stamp).timestamp()


def shot_input(proxy_dir):
    """Plan, timing and context of the shot from the proxy job directory (and, when the proxy launched the analysis,
    its request.json). Works for jobs that predate this module (tonight's shots)."""
    d = Path(proxy_dir)
    meta = _read_json(d / 'job.json', {}) or {}
    progress = _read_json(d / 'progress.json', {}) or {}
    inv = (_read_json(d / 'invocation.json', {}) or {}).get('parameters') or {}
    req = _read_json(analysis_dir(d) / 'request.json', {}) or {}
    shot = progress.get('shot_sweep') or {}
    laser = meta.get('laser')
    laser_id = laser.get('id') if isinstance(laser, dict) else laser
    plan = req.get('plan') or {}
    spec = (inv.get('shot_sweep') or {}).get('plan') if isinstance(inv.get('shot_sweep'), dict) else None
    shape = plan.get('shape') or (spec or {}).get('shape') or inv.get('shape') or 'staircase'
    levels, dwell, settle = [], 0., None
    lead = float(plan.get('lead_s', (spec or {}).get('lead_s', inv.get('lead_s', 0.))) or 0.)
    tail = float(plan.get('tail_s', (spec or {}).get('tail_s', inv.get('tail_s', 0.))) or 0.)
    if plan.get('levels') or plan.get('levels_hz'):
        levels = plan.get('levels') or plan.get('levels_hz')
        dwell, settle = plan.get('dwell_s', 0.), plan.get('settle_s')
    elif spec and spec.get('levels'):
        levels, dwell, settle = spec['levels'], spec.get('dwell_s', 0.), spec.get('settle_s')
    elif inv.get('levels_hz'):
        levels, dwell = inv['levels_hz'], inv.get('dwell_s', 0.)
    # human request 2026-10-23 ~05:10Z: arbitrary-shape shots get RAW data only (no level analysis)
    mode = 'levels' if shape == 'staircase' and levels and dwell else 'raw'
    programmed = req.get('programmed') or shot.get('programmed') or {}
    record = programmed.get('record') or {}
    route = 'rig' if record else 'native'
    edge_s = float(plan.get('edge_s', inv.get('edge_s', 0.01) if route == 'native' else 0.)) if route == 'native' else 0.
    levels_start = float(record.get('levels_start_s', lead))
    record_s = req.get('record_s') or plan.get('record_s') or plan.get('duration_s') or (spec or {}).get('record_s')
    period = float(record.get('period_s') or (float(lead) + float(dwell or 0.) * len(levels) + float(tail) if levels else 0.)
                   or record_s or 0.)
    outcome = req.get('outcome') or shot.get('outcome') or progress.get('outcome') or {}
    laser_doc = None
    try:
        import yaml
        laser_doc = yaml.safe_load((d / 'laser.yaml').read_text())
    except Exception:            # noqa: BLE001
        laser_doc = None
    return dict(proxy_job=d.name, laser=laser_id, mode=mode, shape=shape, plan=plan or spec or {},
                levels=[float(v) for v in levels], dwell=float(dwell or 0.),
                lead=float(lead), tail=float(tail), settle=settle, levels_start=levels_start, period=period,
                record=record, programmed=programmed, edge_s=edge_s, route=route,
                trigger=shot.get('trigger') or req.get('trigger'), request_id=shot.get('request_id') or req.get('request_id'),
                plan_sha256=shot.get('plan_sha256') or req.get('plan_sha256'), lock_job=shot.get('lock_job'),
                fired_unix_s=shot.get('fired_unix_s'), fire_ack_unix_s=shot.get('fire_ack_unix_s'),
                playback_started_unix_s=shot.get('playback_started_unix_s') or outcome.get('playback_started_unix_s'),
                outcome=outcome, target_hz=req.get('target_hz'), lock_dir=req.get('lock_dir'),
                meta=meta, laser_doc=laser_doc, facade_request_id=meta.get('sweep_request_id'))


def lock_target(inp):
    """(target_hz, source): the request's target (what the proxy used), sr422 programmed f0, the laser definition."""
    if inp.get('target_hz'):
        return float(inp['target_hz']), 'proxy request (lock target used for READY)'
    f0 = (inp.get('programmed') or {}).get('f0_hz')
    if f0:
        return float(f0), 'rig owner programmed f0 (firmware target)'
    target = ((inp.get('laser_doc') or {}).get('target') or {}).get('frequency_hz')
    if target:
        return float(target), 'laser definition target (frozen in the proxy job)'
    return None, None


# -------------------------------------------------------------------------------------------------- fetch
def influx_token(path):
    for line in open(path):
        m = re.match(r'\s*token\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    raise ValueError('no InfluxDB token in the configured token_file')


def _iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(int(t))) + ('%.6f' % (t % 1))[1:] + 'Z'


def _parse_ns(s):
    base, frac = (s[:-1].split('.') + ['0'])[:2]
    return calendar.timegm(time.strptime(base, '%Y-%m-%dT%H:%M:%S')) + float('0.' + frac)


def _influx_query(cfg, flux, timeout=20):
    c = cfg['influx']
    req = urllib.request.Request(c['url'].rstrip('/') + '/api/v2/query?org=' + urllib.parse.quote(c['org']),
                                 data=flux.encode(),
                                 headers={'Authorization': 'Token ' + influx_token(c['token_file']),
                                          'Accept': 'application/csv', 'Content-type': 'application/vnd.flux'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return response.read().decode()


def _influx_series(body):
    series, cols = {}, None
    for row in csv.reader(io.StringIO(body)):
        if not row or len(row) < 4:
            cols = None
            continue
        if '_time' in row:
            cols = {k: i for i, k in enumerate(row)}
            continue
        if cols is None:
            continue
        try:
            raw = row[cols['_value']]
            value = {'true': 1.0, 'false': 0.0}.get(raw.lower()) if raw.lower() in ('true', 'false') else float(raw)
            series.setdefault(row[cols['_field']], {})[row[cols['_time']]] = value
        except (ValueError, KeyError, IndexError):
            continue
    return series


def fetch_influx(cfg, host, t0, t1):
    c = cfg['influx']
    fields = ' or '.join('r._field=="%s"' % f for f in FULL_FIELDS + ('adc_raw',))
    flux = ('from(bucket:"%s") |> range(start: %s, stop: %s) |> filter(fn:(r)=> r._measurement=="%s" and r.host=="%s" '
            'and (%s)) |> keep(columns:["_time","_field","_value"])' % (c['bucket'], _iso(t0), _iso(t1), c['measurement'], host, fields))
    started = time.perf_counter()
    series = _influx_series(_influx_query(cfg, flux))
    return _assemble({k: {_parse_ns(t): v for t, v in s.items()} for k, s in series.items()}), time.perf_counter() - started, 1e-6


def _vm_get(cfg, path, params, timeout=20):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = cfg['vm']['url'].rstrip('/') + path + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
    with opener.open(url, timeout=timeout) as response:
        return response.read()


def fetch_vm(cfg, host, t0, t1):
    if cfg['vm'].get('force_flush'):
        try:
            _vm_get(cfg, '/internal/force_flush', None, timeout=5)
        except OSError:
            pass
    names = '|'.join(FULL_FIELDS + ('adc_raw',))
    started = time.perf_counter()
    body = _vm_get(cfg, '/api/v1/export', {'match[]': '{__name__=~"ESP32ServoController_(%s)",host="%s"}' % (names, host),
                                           'start': '%.3f' % t0, 'end': '%.3f' % t1})
    series = {}
    for line in body.splitlines():
        if line:
            d = json.loads(line)
            series[d['metric']['__name__'].split('_', 1)[1]] = {t / 1e3: v for t, v in zip(d['timestamps'], d['values'])}
    return _assemble(series), time.perf_counter() - started, 1e-3


def _assemble(series):
    """{field: {t: v}} -> full-data rows (system_value timestamps) + adc rows, as numpy arrays."""
    import numpy as np
    sv = series.get('system_value', {})
    ts = sorted(sv)
    full = {'t': np.array(ts, float)}
    for f in FULL_FIELDS:
        s = series.get(f, {})
        full[f] = np.array([s.get(t, np.nan) for t in ts], float)
    adc = series.get('adc_raw', {})
    ta = sorted(adc)
    return full, {'t': np.array(ta, float), 'adc_raw': np.array([adc[t] for t in ta], float)}


def fetch_clock(cfg, host, t0, t1, source):
    """ESP32 SNTP state around the shot from the esp32_time_sync series (rule C, interim host-side export):
    the last sample at/before T0 and the first after the record end (10 s cadence)."""
    out = dict(source=None, before=None, after=None)
    try:
        if source == 'influx':
            fields = ' or '.join('r._field=="%s"' % f for f in TIME_FIELDS)
            flux = ('from(bucket:"%s") |> range(start: %s, stop: %s) |> filter(fn:(r)=> r._measurement=="esp32_time_sync" '
                    'and r.host=="%s" and (%s)) |> keep(columns:["_time","_field","_value"])'
                    % (cfg['influx']['bucket'], _iso(t0 - 60), _iso(t1 + 30), host, fields))
            series = {k: {_parse_ns(t): v for t, v in s.items()} for k, s in _influx_series(_influx_query(cfg, flux)).items()}
        else:
            body = _vm_get(cfg, '/api/v1/export', {'match[]': '{__name__=~"esp32_time_sync_(%s)",host="%s"}' % ('|'.join(TIME_FIELDS), host),
                                                   'start': '%.3f' % (t0 - 60), 'end': '%.3f' % (t1 + 30)})
            series = {}
            for line in body.splitlines():
                if line:
                    d = json.loads(line)
                    series[d['metric']['__name__'][len('esp32_time_sync_'):]] = {t / 1e3: v for t, v in zip(d['timestamps'], d['values'])}
    except Exception as error:          # noqa: BLE001 - clock data is report-only
        out['error'] = str(error)[:200]
        return out
    stamps = sorted({t for s in series.values() for t in s})
    if not stamps:
        out['error'] = 'no esp32_time_sync samples around the shot'
        return out
    before = [t for t in stamps if t <= t0]
    after = [t for t in stamps if t >= t1]
    pick = lambda t: None if t is None else dict(unix_s=t, **{f: series.get(f, {}).get(t) for f in TIME_FIELDS})
    out.update(source=source, before=pick(before[-1] if before else None), after=pick(after[0] if after else None))
    b, a = out['before'] or {}, out['after'] or {}
    syncs_b, syncs_a = b.get('sntp_syncs'), a.get('sntp_syncs')
    out['sync_between'] = (syncs_a > syncs_b) if syncs_a is not None and syncs_b is not None else None
    # SNTP corrects every ~60 s; a correction between the samples around the shot moved the ESP32 clock by this much
    out['step_in_window_us'] = (a.get('sntp_last_step_us') if out['sync_between'] else 0.) if out['sync_between'] is not None else None
    steps = [abs(x['sntp_last_step_us']) for x in (b, a) if x and x.get('sntp_last_step_us') is not None]
    out['last_step_us_max_abs'] = max(steps) if steps else None
    out['synced'] = all(x.get('time_synced') == 1 for x in (b, a) if x) if (b or a) else None
    return out


def fetch_with_completeness(cfg, host, t0, t1, record_end, *, sleep=time.sleep, now=time.time, fetchers=None,
                            deadline=None):
    """Rule C: InfluxDB first (complete ~0.2 s after the record end); VictoriaMetrics as the fallback, only after its
    ~3.5 s search lag. Complete = the newest full-data row reaches the window end (- complete_margin_s)."""
    fetchers = fetchers or dict(influx=fetch_influx, vm=fetch_vm)
    deadline = deadline or (now() + float(cfg['max_runtime_s']))
    tried = []
    best = None
    for source in cfg.get('sources') or ['influx', 'vm']:
        wait = float(cfg['influx_wait_s'] if source == 'influx' else cfg['vm_wait_s'])
        ready_at = t1 + wait
        if now() < ready_at:
            sleep(min(max(0., ready_at - now()), max(0., deadline - now())))
        for attempt in range(6 if source == 'influx' else 2):
            if now() > deadline:
                break
            try:
                (full, adc), query_s, res = fetchers[source](cfg, host, t0, t1)
            except Exception as error:      # noqa: BLE001
                tried.append(dict(source=source, error=str(error)[:200]))
                break
            last = float(full['t'].max()) if len(full['t']) else None
            complete = last is not None and last >= t1 - float(cfg['complete_margin_s'])
            tried.append(dict(source=source, rows=int(len(full['t'])), adc_rows=int(len(adc['t'])), last_point_unix_s=last,
                              complete=complete, query_s=query_s, at_unix_s=now()))
            candidate = dict(source=source, full=full, adc=adc, query_s=query_s, resolution_s=res, complete=complete,
                             last_point_unix_s=last)
            if best is None or (complete and not best['complete']) or len(full['t']) > len(best['full']['t']):
                best = candidate
            if complete:
                best = candidate
                best['tried'] = tried
                return best
            sleep(0.25 if source == 'influx' else 1.0)
    if best is not None:
        best['tried'] = tried
    return best or dict(source=None, tried=tried, complete=False)


# ----------------------------------------------------------------------------------------------- analysis
class LevelFunction:
    """Programmed offset L(t) (Hz): levels, linear lead/tail ramps (rig compile_record), the native route's per-level
    edge, an early stop (back at the centre after t_cut); F(t) = its integral."""

    def __init__(self, levels, T0, dwell, lead_ramp_s=0.0, tail_ramp_s=0.0, res=2e-4, edge_s=0.0, t_cut=None):
        import numpy as np
        self.np = np
        self.levels, self.T0, self.dwell, self.edge, self.t_cut = np.asarray(levels, float), T0, dwell, edge_s, t_cut
        self.t_end = T0 + len(self.levels) * dwell
        self.tg = np.arange(T0 - lead_ramp_s - 2.0, self.t_end + tail_ramp_s + 2.0, res)
        self.lg = self.value(self.tg, lead_ramp_s, tail_ramp_s)
        self.fg = np.concatenate([[0.0], np.cumsum(0.5 * (self.lg[1:] + self.lg[:-1]) * res)])

    def value(self, t, lead_ramp_s=0.0, tail_ramp_s=0.0):
        np = self.np
        t = np.asarray(t, float)
        n = len(self.levels)
        k = np.clip(np.floor((t - self.T0) / self.dwell).astype(int), 0, n - 1)
        v = np.where((t >= self.T0) & (t < self.t_end), self.levels[k], 0.0)
        if self.edge > 0:
            prev = np.where(k > 0, self.levels[np.clip(k - 1, 0, n - 1)], 0.0)
            u = (t - self.T0 - k * self.dwell) / self.edge
            on = (t >= self.T0) & (t < self.t_end) & (u < 1)
            v = np.where(on, prev + (self.levels[k] - prev) * np.clip(u, 0, 1), v)
            tail_ramp_s = max(tail_ramp_s, self.edge)
        if lead_ramp_s > 0:
            r = (t >= self.T0 - lead_ramp_s) & (t < self.T0)
            v = np.where(r, self.levels[0] * (t - (self.T0 - lead_ramp_s)) / lead_ramp_s, v)
        if tail_ramp_s > 0:
            r = (t >= self.t_end) & (t < self.t_end + tail_ramp_s)
            v = np.where(r, self.levels[-1] * (1 - (t - self.t_end) / tail_ramp_s), v)
        if self.t_cut is not None:
            v = np.where(t > self.t_cut, 0.0, v)
        return v

    def at(self, t):
        return self.np.interp(t, self.tg, self.lg)

    def mean(self, a, b):
        np = self.np
        w = b - a
        F = lambda x: np.interp(x, self.tg, self.fg)
        return np.where(w > 1e-6, (F(b) - F(a)) / np.where(w > 1e-6, w, 1.0), self.at(b))


def fit_staircase(t_obs, c_obs, shape_u, shape_v, centre, half_range, step, t_lo, t_hi):
    """Grid search of T0: counts = a + b * L(t - T0) (linear least squares per candidate). Returns (T0, a, b, mse, n)."""
    import numpy as np
    sel = (t_obs >= t_lo) & (t_obs <= t_hi)
    t, y = t_obs[sel], c_obs[sel]
    n = float(len(t))
    if n < 3:
        return (centre, 0.0, 0.0, float('inf'), int(n))
    grid = np.arange(centre - half_range, centre + half_range + step / 2, step)
    best = None
    sy, syy = y.sum(), (y * y).sum()
    for chunk in np.array_split(grid, max(1, len(grid) // 500)):
        x = np.interp(t[None, :] - chunk[:, None], shape_u, shape_v, left=0.0, right=0.0)
        sx, sxx, sxy = x.sum(1), (x * x).sum(1), (x * y[None, :]).sum(1)
        den = n * sxx - sx * sx
        ok = den > 0
        bb = np.where(ok, (n * sxy - sx * sy) / np.where(ok, den, 1), 0)
        aa = (sy - bb * sx) / n
        sse = np.where(ok, syy - aa * sy - bb * sxy, np.inf)
        j = int(np.argmin(sse))
        if best is None or sse[j] / n < best[3]:
            best = (float(chunk[j]), float(aa[j]), float(bb[j]), float(sse[j] / n), int(n))
    return best


def fit_response(t_meas, f_off, lf, clip_hz, big_hz=50e6):
    """reading ~ c + g * mean(L over [t-d-w, t-d]). (c, g) from all points (clipped); (d, w) only from 'feature'
    points (lead/tail ramps, jumps >= big_hz): with a monotonic 10 MHz staircase a one-dwell delay is a constant."""
    import numpy as np
    feat = np.abs(lf.at(t_meas - 0.15) - lf.at(t_meas + 0.15)) >= big_hz
    feat |= np.abs(lf.at(t_meas) - lf.at(t_meas - 0.15)) >= big_hz
    d, w, c, g = 0.05, 0.0, 0.0, 1.0
    for _ in range(4):
        x = lf.mean(t_meas - d - w, t_meas - d)
        keep = np.ones(len(x), bool)
        for _ in range(3):
            if keep.sum() < 2:
                break
            A = np.vstack([np.ones(keep.sum()), x[keep]]).T
            (c, g), *_ = np.linalg.lstsq(A, f_off[keep], rcond=None)
            keep = np.abs(f_off - c - g * x) < clip_hz
        if feat.sum() < 2:
            break
        best = None
        for ww in (np.arange(0.0, 0.1201, 0.002) if feat.sum() >= 8 else [0.0]):
            for dd in np.arange(-0.05, 0.1501, 0.001):
                xx = lf.mean(t_meas[feat] - dd - ww, t_meas[feat] - dd)
                sse = float(np.sum((f_off[feat] - c - g * xx) ** 2))
                if best is None or sse < best[0]:
                    best = (sse, float(dd), float(ww))
        _, d, w = best
    x = lf.mean(t_meas - d - w, t_meas - d)
    r = f_off - c - g * x
    sig2 = float(np.mean(np.minimum(r[~feat] ** 2, clip_hz ** 2))) if (~feat).any() else float(np.mean(r ** 2))
    prof = [(dd, float(np.sum((f_off[feat] - c - g * lf.mean(t_meas[feat] - dd - w, t_meas[feat] - dd)) ** 2)))
            for dd in np.arange(-0.05, 0.1501, 0.001)] if feat.sum() >= 2 else []
    lmin = min(v for _, v in prof) if prof else 0.0
    ok = [dd for dd, v in prof if v - lmin <= sig2]
    return dict(delay_s=d, width_s=w, offset_hz=float(c), gain=float(g), delay_range_s=[min(ok), max(ok)] if ok else None,
                n_feature=int(feat.sum()), rms_hz=float(math.sqrt(sig2)),
                feature_rms_hz=float(np.sqrt(np.mean(r[feat] ** 2))) if feat.any() else None)


def analyse(inp, data, cfg, *, clock_info=None, adc_log=None, now=time.time):
    """The procedure on fetched data -> the ldcon.shot-analysis/v1 document (no I/O)."""
    import numpy as np
    full, adc = data['full'], data['adc']
    levels = np.asarray(inp['levels'], float)
    n_lv, dwell = len(levels), float(inp['dwell'])
    t0w, t1w = data['window']
    rec = inp.get('record') or {}
    lead_ramp, tail_ramp = float(rec.get('lead_ramp_s', 0.0)), float(rec.get('tail_ramp_s', 0.0))
    quality, warnings = {}, []
    # ---- staircase detection from mod_raw (full rows) + adc_raw (threshold rows)
    t_obs = np.concatenate([full['t'], adc['t']])
    c_obs = np.concatenate([full['mod_raw'], adc['adc_raw']])
    good = np.isfinite(c_obs)
    mr = full['mod_raw'][np.isfinite(full['mod_raw'])]
    if len(mr):
        good &= (c_obs >= mr.min() - 200) & (c_obs <= mr.max() + 200)
    glitches = [dict(t=float(t), counts=float(c)) for t, c in zip(t_obs[~good], c_obs[~good]) if np.isfinite(c)]
    t_obs, c_obs = t_obs[good], c_obs[good]
    if len(t_obs) < 10 or n_lv == 0:
        raise ValueError('too few ADC rows in the shot window (%d)' % len(t_obs))
    base_m = mr[:max(5, len(mr) // 10)]
    noise0 = max(float(np.std(base_m)), 1.0) if len(base_m) else 2.0
    o = np.argsort(t_obs)
    jumps = [float(t_obs[o][j]) + 1e-4 for j in np.where(np.abs(np.diff(c_obs[o])) > 1000)[0]]
    candidates = [t1w] + sorted(jumps, reverse=True)
    nominal, half = data['nominal_start'], data['search_half_s']
    steps = np.abs(np.diff(levels))
    min_step = float(steps[steps > 0].min()) if (steps > 0).any() else 0.0

    def fit_edges(t_end):
        out = []
        for edge in sorted({0.0, float(inp.get('edge_s') or 0.0)}):
            shp = LevelFunction(levels, 0.0, dwell, lead_ramp, tail_ramp, edge_s=edge)
            coarse = fit_staircase(t_obs, c_obs, shp.tg, shp.lg, nominal, half, 0.002, t0w, t_end)
            fine = fit_staircase(t_obs, c_obs, shp.tg, shp.lg, coarse[0], 0.006, 0.0001, t0w, t_end)
            model = lambda tt, f=fine, sh=shp: f[1] + f[2] * np.interp(tt - f[0], sh.tg, sh.lg, left=0.0, right=0.0)
            r = c_obs - model(t_obs)
            inw = t_obs <= t_end
            mad = 1.4826 * float(np.median(np.abs(r[inw] - np.median(r[inw])))) if inw.any() else 0.0
            thr = max(8 * mad, 30.0)
            win = np.array([model(t_obs + dt) for dt in np.arange(-3e-3, 3.01e-3, 2e-4)])
            lo, hi = win.min(0), win.max(0)
            near = np.where(c_obs < lo, lo - c_obs, np.where(c_obs > hi, c_obs - hi, 0.0))
            keep = (np.abs(r) <= thr) | (near <= thr) | ~inw
            if (~keep).any():
                coarse = fit_staircase(t_obs[keep], c_obs[keep], shp.tg, shp.lg, fine[0], 0.05, 0.001, t0w, t_end)
                fine = fit_staircase(t_obs[keep], c_obs[keep], shp.tg, shp.lg, coarse[0], 0.006, 0.0001, t0w, t_end)
            out.append((fine[3], edge, fine, shp, keep))
        out.sort(key=lambda x: x[0])
        return out

    tried = []
    fits, t_end = None, t1w
    for t_end in candidates[:6]:
        fits = fit_edges(t_end)
        rms = float(np.sqrt(max(fits[0][0], 0)))
        tried.append(dict(t_end=t_end, rms_counts=rms))
        if rms < max(10 * noise0, 0.3 * abs(fits[0][2][2]) * min_step):
            break
    t_cut = t_end if t_end < t1w else None
    _, edge, (T0, a, b, mse, _n), shp, keep = fits[0]
    glitches += [dict(t=float(t), counts=float(c)) for t, c in zip(t_obs[~keep], c_obs[~keep])]
    t_obs, c_obs = t_obs[keep], c_obs[keep]
    resid = c_obs - (a + b * np.interp(t_obs - T0, shp.tg, shp.lg, left=0.0, right=0.0))
    resid = np.where(t_obs > (t_cut or np.inf), 0.0, resid)
    inside = (t_obs >= T0) & (t_obs < min(T0 + n_lv * dwell, t_cut or np.inf))
    played = int(np.clip(np.ceil((t_cut - T0) / dwell), 0, n_lv)) if t_cut else n_lv
    pre = full['t'] < T0 - lead_ramp - 0.1
    adc_noise = None
    if pre.sum() >= 5:
        m = full['mod_raw'][pre]
        m = m[np.isfinite(m)]
        if len(m) >= 5:
            adc_noise = dict(n=int(len(m)), std_counts=float(np.std(m)), p2p_counts=float(np.ptp(m)), median_counts=float(np.median(m)))
    noise = (adc_noise or {}).get('std_counts', 2.0)
    min_step_counts = float(abs(b) * min_step)
    clip = max(4 * noise, 3.0)
    frac_ok = float(np.mean(np.abs(resid[inside]) < max(clip, 0.25 * min_step_counts))) if inside.any() else 0.0
    span_counts = float(abs(b) * np.ptp(levels))
    detected = bool(inside.sum() >= 8 and frac_ok >= 0.8 and span_counts > 20 * max(noise, 1.0))
    T = T0 + dwell * np.arange(n_lv)
    # ---- did each step get a threshold-logged adc_raw row?
    missed, adc_seen = 0, []
    for i in range(n_lv):
        sel = (adc['t'] >= T[i]) & (adc['t'] < T[i] + dwell)
        adc_seen.append(bool(sel.any()))
        if 0 < i < played and abs(levels[i] - levels[i - 1]) > 0 and not sel.any():
            missed += 1
    # ---- wavemeter points
    new = full['is_new_system_value'] == 1
    t_rx = full['t'][new]
    lat = full['system_latency_us'][new]
    t_meas = t_rx - np.where(np.isfinite(lat) & (lat >= 0) & (lat < 1e5), lat, 0) * 1e-6
    nm = full['system_value'][new]
    okv = np.isfinite(nm) & (nm > 0)
    t_rx, lat, t_meas, nm = t_rx[okv], lat[okv], t_meas[okv], nm[okv]
    f = C / (nm * 1e-9)
    pre_pts = t_meas < T0 - lead_ramp - 0.1
    if pre_pts.sum() >= 3:
        f_ref, ref_src = float(np.median(f[pre_pts])), 'median of the wavemeter points before the record moved'
    else:
        f_ref, ref_src = float(np.median(f)) if len(f) else float('nan'), 'median of all points (fewer than 3 before the record)'
        warnings.append('reference from all points: fewer than 3 wavemeter points before the record')
    f_off = f - f_ref
    target_hz, target_src = lock_target(inp)
    lf = LevelFunction(levels, T0, dwell, lead_ramp, tail_ramp, edge_s=edge,
                       t_cut=t_cut if t_cut is not None and t_cut < T0 + n_lv * dwell else None)
    in_span = (t_meas >= T0 - lead_ramp - 0.2) & (t_meas < T0 + n_lv * dwell + tail_ramp + 0.2)
    resp = None
    sig = None
    if detected and in_span.sum() >= 10:
        sig = 1.4826 * float(np.median(np.abs(f_off[pre_pts] - np.median(f_off[pre_pts])))) if pre_pts.sum() >= 3 else 0.0
        clip_hz = max(5 * sig, 0.5 * min_step, 5e6)
        resp = fit_response(t_meas[in_span], f_off[in_span], lf, clip_hz)
        resp['clip_hz'] = clip_hz
        resp['n'] = int(in_span.sum())
    prior = float(cfg.get('prior_delay_s', 0.025))
    d_s = resp['delay_s'] if resp else prior
    w_s = resp['width_s'] if resp else 0.0
    d_lo, d_hi = (resp['delay_range_s'] if resp and resp.get('delay_range_s') else [d_s, d_s])
    delay_source = 'fit'
    if not resp or d_hi - d_lo > 0.03:
        delay_source = ('prior (this shot constrains the delay only to %.3f..%.3f s)' % (d_lo, d_hi)) if resp else 'prior (no fit)'
        d_s, w_s = prior, 0.0
        d_lo, d_hi = d_s - 0.01, d_s + 0.01
    settle = cfg.get('settle_s')
    settle = float(settle if settle is not None else (inp.get('settle') if inp.get('settle') is not None else 0.03))
    edges = np.concatenate([T, [T0 + n_lv * dwell]])
    win_a, win_b = t_meas - d_s - w_s, t_meas - d_s
    ca, cb = t_meas - d_hi - w_s, t_meas - d_lo
    in_tr = ((ca[:, None] <= edges[None, :] + settle) & (cb[:, None] >= edges[None, :])).any(1) if len(t_meas) else np.zeros(0, bool)
    if lead_ramp > 0 or tail_ramp > 0:
        # a reading that sees the lead or tail ramp is in a transition too (the offset is moving)
        on_ramp = ((cb >= T0 - lead_ramp) & (ca < T0)) | ((cb >= edges[-1]) & (ca < edges[-1] + tail_ramp))
        in_tr = in_tr | on_ramp
    k_eff = np.clip(np.floor((win_b - T0) / dwell).astype(int), -1, n_lv) if len(t_meas) else np.zeros(0, int)
    if t_cut is not None and played < n_lv:
        k_eff = np.where(win_b > t_cut, n_lv, k_eff)
    lev_hz = lf.mean(win_a, win_b)
    t_since = win_b - np.where((k_eff >= 0) & (k_eff < n_lv), T[np.clip(k_eff, 0, n_lv - 1)], np.nan)
    err = f_off - lev_hz                                           # vs the programmed offset (gain 1, no fit)
    err_fit = f_off - (resp['gain'] * lev_hz + resp['offset_hz'] if resp else lev_hz)
    settled = (k_eff >= 0) & (k_eff < n_lv) & ~in_tr
    # ---- level map
    level_rows = []
    for i in range(n_lv):
        s = (k_eff == i) & ~in_tr
        e = err[s]
        tgt = (target_hz + levels[i]) if target_hz else None
        level_rows.append(dict(level_index=i, offset_hz=float(levels[i]), target_frequency_hz=tgt,
                               target_wavelength_nm=(C / tgt * 1e9) if tgt else None,
                               t_start_unix_s=float(T[i]), t_end_unix_s=float(T[i] + dwell),
                               played=bool(i < played), adc_line_seen=adc_seen[i],
                               n_points=int((k_eff == i).sum()), n_settled=int(s.sum()),
                               mean_error_hz=float(np.mean(e)) if len(e) else None,
                               max_abs_error_hz=float(np.max(np.abs(e))) if len(e) else None))
    allerr = err[settled]
    with_pt = sum(1 for r in level_rows if r['n_settled'])
    summary = dict(levels_total=n_lv, levels_played=played, levels_with_settled_point=with_pt,
                   settled_points=int(settled.sum()), transition_points=int(((k_eff >= 0) & (k_eff < n_lv) & in_tr).sum()),
                   points_total=int(len(t_meas)),
                   rms_error_hz=float(np.sqrt(np.mean(allerr ** 2))) if len(allerr) else None,
                   max_abs_error_hz=float(np.max(np.abs(allerr))) if len(allerr) else None,
                   rms_error_fit_hz=float(np.sqrt(np.mean(err_fit[settled] ** 2))) if settled.any() else None,
                   error_definition='wavemeter offset from the pre-record reference minus the programmed offset, '
                                    'on points outside transitions (gain 1, no fitted offset)')
    g = (resp or {}).get('gain')
    lat_ok = lat[np.isfinite(lat)]
    fired = inp.get('fired_unix_s')
    quality.update(staircase_detected=detected, record_complete=bool(played >= n_lv), levels_played=played,
                   laser_followed=bool(g is not None and abs(g - 1) < 0.1), realised_gain=g,
                   delay_from_this_shot=(delay_source == 'fit'), adc_glitches=len(glitches),
                   adc_steps_without_change_line=missed,
                   wavemeter_points_per_level=float(((k_eff >= 0) & (k_eff < n_lv)).sum()) / n_lv,
                   levels_with_settled_point=with_pt, data_complete=bool(data.get('complete')),
                   clock_synced=(clock_info or {}).get('synced'), clock_sync_between=(clock_info or {}).get('sync_between'),
                   clock_step_in_window_us=(clock_info or {}).get('step_in_window_us'))
    if not data.get('complete'):
        warnings.append('the data window was not complete when fetched (newest point %s, window end %.3f)' % (data.get('last_point_unix_s'), t1w))
    step_us = (clock_info or {}).get('step_in_window_us')
    if step_us is not None and abs(step_us) > CLOCK_STEP_WARN_US:
        warnings.append('an SNTP correction of %.0f us happened around the shot (ESP32 time may jump inside the window)' % step_us)
    if not detected:
        warnings.append('the programmed staircase was not detected in mod_raw/adc_raw; per-point indices are not meaningful')
    quality['ok'] = bool(detected and data.get('complete') and quality['record_complete'])
    quality['warnings'] = warnings
    points = dict(t_unix_s=t_meas, t_rx_unix_s=t_rx, wavelength_nm=nm, offset_hz=f_off,
                  level_index=k_eff if detected else [None] * len(t_meas),
                  in_transition=in_tr if detected else [None] * len(t_meas),
                  t_since_transition_s=t_since if detected else [None] * len(t_meas),
                  programmed_offset_hz=lev_hz, error_hz=err, error_fit_hz=err_fit)
    points = {k: [None if (isinstance(x, float) and not math.isfinite(x)) else x for x in clean(list(v))] for k, v in points.items()}
    points['level_index'] = [int(x) if x is not None else None for x in points['level_index']]
    points['in_transition'] = [bool(x) if x is not None else None for x in points['in_transition']]
    laser_doc = inp.get('laser_doc') or {}
    meta = inp.get('meta') or {}
    revisions = dict(firmware_version=_firmware_version(laser_doc), laser_revision=meta.get('laser_revision'),
                     set_revision=meta.get('set_revision'), job_revision=meta.get('revision'),
                     profile_revision=_profile_revision(laser_doc), analysis_version=analysis_version())
    temp = dict(adc_log_threshold=adc_log, target_time_match=_time_match(laser_doc, inp))
    doc = dict(schema=CONTRACT, mode='levels', shape=inp.get('shape', 'staircase'),
               analysis_version=revisions['analysis_version'], created_unix_s=now(),
               laser=inp.get('laser'), proxy_job=inp['proxy_job'], sweep_id=inp['proxy_job'], request_id=inp.get('request_id'),
               facade_request_id=inp.get('facade_request_id'), plan_sha256=inp.get('plan_sha256'), lock_job=inp.get('lock_job'),
               outcome=dict(verdict=(inp.get('outcome') or {}).get('verdict'), reason=(inp.get('outcome') or {}).get('reason'),
                            playback_completed=(inp.get('outcome') or {}).get('playback_completed')),
               time_base='ESP32 unix time (SNTP); wavemeter point time = receive time - system_latency_us',
               source=data.get('source'), query_s=data.get('query_s'), timestamp_resolution_s=data.get('resolution_s'),
               completeness=dict(complete=bool(data.get('complete')), window_unix_s=[t0w, t1w],
                                 last_point_unix_s=data.get('last_point_unix_s'), full_rows=int(len(full['t'])),
                                 adc_rows=int(len(adc['t'])), fetch_attempts=data.get('tried')),
               t0_unix_s=T0, summary=summary, levels=level_rows, points=points,
               parameters=dict(t0_unix_s=T0, dwell_s=dwell, lead_s=inp.get('lead'), tail_s=inp.get('tail'),
                               levels_start_s=inp.get('levels_start'), record_s=inp.get('period'),
                               lead_ramp_s=lead_ramp, tail_ramp_s=tail_ramp, edge_s=edge,
                               delay_s=d_s, delay_range_s=[d_lo, d_hi], delay_source=delay_source, width_s=w_s,
                               settle_s=settle,
                               transition_rule='in_transition if the reading window [t - delay_hi - width, t - delay_lo] '
                                               'overlaps [T_i, T_i + settle_s] for a level start T_i (or the record end), or '
                                               'sees the lead/tail ramp; level_index from t - delay',
                               gain=g, offset_hz=(resp or {}).get('offset_hz'), counts_per_hz=b, counts_at_zero=a,
                               hz_per_count=(1 / b if b else None),
                               reference=dict(f_ref_hz=f_ref, f_ref_source=ref_src, lock_target_hz=target_hz,
                                              lock_target_source=target_src,
                                              f_ref_minus_target_hz=(f_ref - target_hz) if target_hz else None,
                                              f_ref_robust_std_hz=sig)),
               staircase_fit=dict(rms_counts=float(np.sqrt(max(mse, 0))), n_obs_in_span=int(inside.sum()),
                                  frac_obs_consistent=frac_ok, min_step_counts=min_step_counts, span_counts=span_counts,
                                  t_cut_unix_s=t_cut, fit_window_candidates=tried, adc_noise=adc_noise,
                                  adc_glitches_rejected=glitches[:50], response=resp),
               revisions=revisions, temporary_settings=temp,
               clock=dict(esp32=clock_info, fired_unix_s=fired, fire_ack_unix_s=inp.get('fire_ack_unix_s'),
                          t0_minus_fired_s=(T0 - fired - float(inp.get('levels_start') or 0.)) if fired else None,
                          playback_detected_unix_s=inp.get('playback_started_unix_s'),
                          playback_detected_minus_t0_s=(inp['playback_started_unix_s'] - T0) if inp.get('playback_started_unix_s') else None,
                          wavemeter_latency_us_median=float(np.median(lat_ok)) if len(lat_ok) else None,
                          wavemeter_latency_us_max=float(np.max(lat_ok)) if len(lat_ok) else None,
                          wavemeter_interval_median_s=float(np.median(np.diff(t_rx))) if len(t_rx) > 2 else None,
                          note='the shot time origin is t0_unix_s (fitted); playback_detected is 0.5-2.4 s late and '
                               'fired_unix_s is the host clock'),
               quality=quality)
    return clean(doc)


def commanded_sequence(inp, target_hz):
    """The commanded offset sequence actually programmed, relative to the record start (t_rel_s = 0 at the trigger), from
    the plan the proxy sent (times_s + offsets_hz | samples at sample_rate_hz | points [{t_s, offset_hz}] | levels +
    dwell + lead). Absolute times are the HOST trigger time + t_rel_s (host clock; not fitted to the ESP32 ADC)."""
    plan = inp.get('plan') or {}
    t, v, form = None, None, None
    values = next((plan[k] for k in ('offsets_hz', 'values_hz', 'samples_hz', 'waveform_hz') if isinstance(plan.get(k), list)), None)
    if isinstance(plan.get('times_s'), list) and values is not None and len(plan['times_s']) == len(values):
        t, v, form = [float(x) for x in plan['times_s']], [float(x) for x in values], 'times_s + offsets'
    elif values is not None and (plan.get('sample_rate_hz') or plan.get('dt_s')):
        dt = float(plan['dt_s']) if plan.get('dt_s') else 1. / float(plan['sample_rate_hz'])
        t, v, form = [i * dt for i in range(len(values))], [float(x) for x in values], 'samples at %.6g s' % dt
    elif isinstance(plan.get('points'), list) and plan['points'] and isinstance(plan['points'][0], dict):
        t = [float(p.get('t_s', p.get('time_s'))) for p in plan['points']]
        v = [float(p.get('offset_hz', p.get('value_hz'))) for p in plan['points']]
        form = 'points'
    elif inp.get('levels') and inp.get('dwell'):
        lead = float(inp.get('levels_start') or 0.)
        t = [0.] + [lead + i * inp['dwell'] for i in range(len(inp['levels']))] + [lead + len(inp['levels']) * inp['dwell']]
        v = [0.] + list(inp['levels']) + [0.]
        form = 'levels (step changes at t_rel_s)'
    if t is None:
        return dict(available=False, reason='the programmed sequence is not in a known plan form', plan_keys=sorted(plan))
    fired = inp.get('fired_unix_s')
    return dict(available=True, form=form, t_rel_s=t, offset_hz=v,
                target_frequency_hz=[target_hz + x for x in v] if target_hz else None,
                target_wavelength_nm=[C / (target_hz + x) * 1e9 for x in v] if target_hz else None,
                t_unix_s_host=[fired + x for x in t] if fired else None,
                time_base='t_rel_s from the record start (trigger); t_unix_s_host = host trigger time + t_rel_s')


def raw_document(inp, data, cfg, *, clock_info=None, adc_log=None, now=time.time):
    """Raw mode (arbitrary shapes, human request 2026-10-23 ~05:10Z): the measured series and the programmed sequence
    with their own timestamps, plus metadata and completeness. No level fit, no per-point classification."""
    import numpy as np
    full, adc = data['full'], data['adc']
    new = full['is_new_system_value'] == 1
    t_rx = full['t'][new]
    lat = full['system_latency_us'][new]
    nm = full['system_value'][new]
    ok = np.isfinite(nm) & (nm > 0)
    t_rx, lat, nm, seq = t_rx[ok], lat[ok], nm[ok], full['system_seq'][new][ok]
    t_meas = t_rx - np.where(np.isfinite(lat) & (lat >= 0) & (lat < 1e5), lat, 0) * 1e-6
    target_hz, target_src = lock_target(inp)
    laser_doc, meta = inp.get('laser_doc') or {}, inp.get('meta') or {}
    t0w, t1w = data['window']
    warnings = []
    if not data.get('complete'):
        warnings.append('the data window was not complete when fetched (newest point %s, window end %.3f)' % (data.get('last_point_unix_s'), t1w))
    step_us = (clock_info or {}).get('step_in_window_us')
    if step_us is not None and abs(step_us) > CLOCK_STEP_WARN_US:
        warnings.append('an SNTP correction of %.0f us happened around the shot (ESP32 time may jump inside the window)' % step_us)
    commanded = commanded_sequence(inp, target_hz)
    if not commanded['available']:
        warnings.append('commanded sequence unavailable: ' + commanded['reason'])
    revisions = dict(firmware_version=_firmware_version(laser_doc), laser_revision=meta.get('laser_revision'),
                     set_revision=meta.get('set_revision'), job_revision=meta.get('revision'),
                     profile_revision=_profile_revision(laser_doc), analysis_version=analysis_version())
    mr = np.isfinite(full['mod_raw'])
    doc = dict(schema=CONTRACT, mode='raw', shape=inp.get('shape'), analysis_version=revisions['analysis_version'],
               created_unix_s=now(), laser=inp.get('laser'), proxy_job=inp['proxy_job'], sweep_id=inp['proxy_job'],
               request_id=inp.get('request_id'), facade_request_id=inp.get('facade_request_id'),
               plan_sha256=inp.get('plan_sha256'), lock_job=inp.get('lock_job'),
               outcome=dict(verdict=(inp.get('outcome') or {}).get('verdict'), reason=(inp.get('outcome') or {}).get('reason'),
                            playback_completed=(inp.get('outcome') or {}).get('playback_completed')),
               time_base='ESP32 unix time (SNTP) for measured series; wavemeter t_unix_s = receive time - system_latency_us',
               source=data.get('source'), query_s=data.get('query_s'), timestamp_resolution_s=data.get('resolution_s'),
               completeness=dict(complete=bool(data.get('complete')), window_unix_s=[t0w, t1w],
                                 last_point_unix_s=data.get('last_point_unix_s'), full_rows=int(len(full['t'])),
                                 adc_rows=int(len(adc['t'])), fetch_attempts=data.get('tried')),
               series=dict(wavelength=dict(t_unix_s=t_meas, t_rx_unix_s=t_rx, wavelength_nm=nm, system_seq=seq,
                                           latency_us=lat),
                           mod_raw=dict(t_unix_s=full['t'][mr], counts=full['mod_raw'][mr],
                                        note='held ADC value of the full-data row (sampled 0-1 ms after the row time)'),
                           adc_raw=dict(t_unix_s=adc['t'], counts=adc['adc_raw'],
                                        note='physical ADC, logged only on a change >= log_adc_change_threshold')),
               commanded=commanded,
               parameters=dict(record_s=inp.get('period'), lead_s=inp.get('lead'), tail_s=inp.get('tail'),
                               reference=dict(lock_target_hz=target_hz, lock_target_source=target_src)),
               summary=dict(wavelength_points=int(len(t_meas)), mod_raw_points=int(mr.sum()), adc_raw_points=int(len(adc['t'])),
                            commanded_points=len(commanded.get('t_rel_s') or [])),
               revisions=revisions, temporary_settings=dict(adc_log_threshold=adc_log, target_time_match=_time_match(laser_doc, inp)),
               clock=dict(esp32=clock_info, fired_unix_s=inp.get('fired_unix_s'), fire_ack_unix_s=inp.get('fire_ack_unix_s'),
                          playback_detected_unix_s=inp.get('playback_started_unix_s'),
                          wavemeter_latency_us_median=float(np.median(lat[np.isfinite(lat)])) if np.isfinite(lat).any() else None,
                          note='no record start is fitted in raw mode; playback_detected is 0.5-2.4 s late, fired is host time'),
               quality=dict(ok=bool(data.get('complete')) and len(t_meas) > 0, data_complete=bool(data.get('complete')),
                            commanded_available=commanded['available'], clock_synced=(clock_info or {}).get('synced'),
                            clock_step_in_window_us=step_us, warnings=warnings))
    return clean(doc)


def _firmware_version(laser_doc):
    for dev in (laser_doc.get('devices') or {}).values():
        cs = dev.get('control_settings') or {}
        for block_name in ('native_ff_sweep', 'steady_lock', 'native_current'):
            v = (cs.get(block_name) or {}).get('firmware_version') if isinstance(cs.get(block_name), dict) else None
            if v:
                return v
        if cs.get('firmware_version'):
            return cs['firmware_version']
    return None


def _profile_revision(laser_doc):
    for dev in (laser_doc.get('devices') or {}).values():
        cs = dev.get('control_settings') or {}
        for name in ('native_ff_sweep', 'locked_sweep'):
            if isinstance(cs.get(name), dict) and cs[name].get('revision'):
                return dict(name=name, revision=cs[name]['revision'])
    return None


def _time_match(laser_doc, inp):
    """target_time_match / target_delay_us the owner sets during the shot (sr422: locked_sweep.firmware; sr1092: the
    owner sets target_time_match=1 with the FF multipliers and leaves target_delay_us)."""
    for dev in (laser_doc.get('devices') or {}).values():
        fw = ((dev.get('control_settings') or {}).get('locked_sweep') or {}).get('firmware')
        if isinstance(fw, dict):
            return dict(target_time_match=fw.get('target_time_match'), target_delay_us=fw.get('target_delay_us'),
                        source='laser definition locked_sweep.firmware (set at arm, restored after)')
    if inp.get('route') == 'native':
        return dict(target_time_match=1, target_delay_us=None,
                    source='native owner: target_time_match=1 while the FF multipliers are on; target_delay_us unchanged')
    return None


QUALITY_POINT_COLUMNS = (('quality_single_mode', 'single_mode'), ('quality_degraded', 'degraded'),
                         ('quality_side_peak_height_max', 'side_peak_height_max'),
                         ('quality_side_peak_prominence_max', 'side_peak_prominence_max'),
                         ('quality_main_fwhm_fraction', 'main_fwhm_fraction'))
QUALITY_METRICS = ('side_peak_height_max', 'side_peak_prominence_max', 'main_fwhm_fraction', 'near_main_peak_prominence_max')


def owner_quality_rows(inp, proxy_dir, t_lo, t_hi):
    """The lock-side session's owner samples (``sweep_sample`` events of the lock job) in [t_lo, t_hi]: (rows, source)
    or ([], reason). Read-only, streamed."""
    lock_dir = inp.get('lock_dir') or (str(Path(proxy_dir).parent / inp['lock_job']) if inp.get('lock_job') else None)
    if not lock_dir:
        return [], 'the lock job of the shot is unknown'
    path = Path(lock_dir) / 'telemetry.events.jsonl'
    rows = []
    try:
        with open(path, 'rb') as stream:
            for line in stream:
                if b'"sweep_sample"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                row = event.get('payload') if event.get('kind') == 'sweep_sample' else None
                t = (row or {}).get('unix_s')
                if isinstance(t, (int, float)) and t_lo <= t <= t_hi:
                    rows.append(row)
    except FileNotFoundError:
        return [], 'no lock-job event log (%s)' % path
    except OSError as error:
        return [], 'lock-job event log unreadable: %s' % error
    if not rows:
        return [], 'no owner samples in the shot window (a route without per-sample mode metrics)'
    return rows, str(path)


def attach_quality(doc, inp, rows, source):
    """Per-point quality columns (nearest owner sample within a tolerance) + doc['mode_quality']."""
    from ldcon.jobs.sweep_tracking import degradation_from_rows
    if doc.get('mode') == 'levels':
        points = doc.get('points') or {}
    else:
        points = ((doc.get('series') or {}).get('wavelength')) or {}
    if not rows:
        doc['mode_quality'] = dict(available=False, reason=source)
        return doc
    def t_of(r):
        a = r.get('acquired_unix_s')
        return float(a) if isinstance(a, (int, float)) and math.isfinite(a) else float(r['unix_s'])
    rows = sorted(rows, key=t_of)
    ts = [t_of(r) for r in rows]
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    tol = min(.5, max(.05, .75 * gaps[len(gaps) // 2])) if gaps else .25
    import bisect
    t_points = list(points.get('t_unix_s') or [])
    cols = {name: [] for name, _ in QUALITY_POINT_COLUMNS}
    cols['quality_match_dt_s'] = []
    matched = degraded = 0
    for t in t_points:
        best = None
        if isinstance(t, (int, float)):
            i = bisect.bisect_left(ts, t)
            cands = [j for j in (i - 1, i) if 0 <= j < len(ts)]
            j = min(cands, key=lambda j: abs(ts[j] - t)) if cands else None
            if j is not None and abs(ts[j] - t) <= tol:
                best = j
        row = rows[best] if best is not None else None
        metrics = (row or {}).get('metrics') or {}
        for name, key in QUALITY_POINT_COLUMNS:
            value = (row or {}).get(key) if key in ('single_mode', 'degraded') else metrics.get(key)
            cols[name].append(value if row is not None else None)
        cols['quality_match_dt_s'].append((t - ts[best]) if row is not None else None)
        if row is not None:
            matched += 1
            degraded += row.get('degraded') is True
    points.update(clean(cols))
    owner = dict(unix_s=[r.get('unix_s') for r in rows], acquired_unix_s=[r.get('acquired_unix_s') for r in rows],
                 phase=[r.get('phase') for r in rows], level_index=[r.get('level_index') for r in rows],
                 commanded_offset_hz=[r.get('commanded_offset_hz') for r in rows],
                 frequency_hz=[r.get('frequency_hz') for r in rows], valid=[r.get('valid') for r in rows],
                 single_mode=[r.get('single_mode') for r in rows], degraded=[r.get('degraded') for r in rows],
                 maintenance_required=[r.get('maintenance_required') for r in rows],
                 degraded_by=[r.get('degraded_by') for r in rows],
                 **{k: [((r.get('metrics') or {}).get(k)) for r in rows] for k in QUALITY_METRICS})
    degradation = degradation_from_rows(rows, inp.get('playback_started_unix_s'))
    doc['mode_quality'] = clean(dict(
        available=True, source=source, match='nearest owner sample (wavemeter frame time acquired_unix_s) within '
        'match_tolerance_s of each wavemeter point', match_tolerance_s=tol, points_total=len(t_points),
        points_matched=matched, points_degraded=degraded, owner_samples=owner, degradation=degradation))
    if isinstance(doc.get('summary'), dict):
        doc['summary']['mode_degraded_points'] = degraded
        doc['summary']['mode_degraded_samples'] = degradation.get('count')
    if doc.get('mode') == 'levels' and isinstance(doc.get('levels'), list):
        per = {}
        for k, d in zip(points.get('level_index') or [], cols['quality_degraded']):
            if k is not None and d is not None:
                n = per.setdefault(k, [0, 0])
                n[0] += 1
                n[1] += d is True
        for row in doc['levels']:
            n = per.get(row.get('index', row.get('level_index')))
            row['quality_points'] = n[0] if n else 0
            row['degraded_points'] = n[1] if n else 0
    if degradation.get('count'):
        w = degradation.get('worst') or {}
        doc.setdefault('quality', {}).setdefault('warnings', []).append(
            'mode obviously degraded on %d of %s playback samples (first t=%s s, worst %s=%s at level %s); the shot was '
            'not aborted for it (quality index, see mode_quality and the quality_* point columns)'
            % (degradation['count'], degradation.get('playback_valid_samples'), (degradation.get('first') or {}).get('t_s'),
               w.get('metric'), w.get('value'), w.get('level_index')))
    return doc


def owner_adc_log(cfg, inp, t_lo, t_hi):
    """The owner's temporary log_adc_change_threshold record for this shot (adc-log history JSONL written by the owner):
    the latest entry applied in [arm - 120 s, T0] (matched by request_id when the owner knows it)."""
    path = cfg.get('owner_adc_log_history')
    if not path:
        return None
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - (2 << 20)))
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return None                     # the owner never changed the threshold (or predates the adc-log journal)
    except OSError as error:
        return dict(error='owner adc-log history unreadable: %s' % error)
    best = None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = row.get('applied_unix_s') or row.get('planned_unix_s')
        if row.get('request_id') and inp.get('request_id') and row['request_id'] == inp['request_id']:
            best = row
            continue
        if isinstance(stamp, (int, float)) and t_lo <= stamp <= t_hi:
            best = row
    return best


# ---------------------------------------------------------------------------------------------- the action
def run(proxy_dir, *, cfg=None, fetchers=None, sleep=time.sleep, now=time.time, clock_fetch=None, attempt_by='proxy',
        publish_dir=None, mode='auto'):
    """Analyse one proxy job's shot and publish <publish_dir>/shot-analysis/{result,status}.json (publish_dir defaults to
    the proxy job itself, where the sweep API reads it). Returns the status."""
    proxy_dir = Path(proxy_dir)
    out = Path(publish_dir) if publish_dir is not None else proxy_dir
    started = now()
    write_status(out, schema=CONTRACT, state='running', ref=ref_for(proxy_dir.name), started_unix_s=started,
                 pid=os.getpid(), attempt_by=attempt_by, proxy_job=proxy_dir.name)
    try:
        cfg = cfg or load_config(state_root_of(proxy_dir))
        if cfg is None:
            raise LookupError('post-shot analysis is not configured for this laser (%s missing)' % CONFIG_NAME)
        inp = shot_input(proxy_dir)
        if mode in ('levels', 'raw'):
            if mode == 'levels' and inp['mode'] != 'levels':
                raise LookupError('level analysis needs a staircase (levels + dwell); this shot is %r' % inp['shape'])
            inp['mode'] = mode
        if inp['period'] <= 0:
            raise LookupError('the record length of this shot is unknown')
        anchor_fired = inp.get('fired_unix_s')
        if anchor_fired:
            nominal, half = anchor_fired + inp['levels_start'], 1.0
        elif inp.get('playback_started_unix_s'):
            nominal, half = inp['playback_started_unix_s'], 3.5
        else:
            raise LookupError('no fired or detected playback time; nothing to analyse')
        record_start = nominal - inp['levels_start']
        t0 = record_start - float(cfg['pre_s']) - (half if not anchor_fired else 0.)
        record_end = record_start + inp['period'] + (half if not anchor_fired else 0.5)
        t1 = record_end + float(cfg['post_s'])
        deadline = started + float(cfg['max_runtime_s'])
        fetched = fetch_with_completeness(cfg, cfg['esp32_host'], t0, t1, record_end, sleep=sleep, now=now,
                                          fetchers=fetchers, deadline=deadline)
        if not fetched.get('source') or fetched.get('full') is None:
            raise LookupError('no ESP32 telemetry could be fetched: %s' % json.dumps(fetched.get('tried'))[:400])
        fetched.update(window=(t0, t1), nominal_start=nominal, search_half_s=half)
        clock_info = (clock_fetch or fetch_clock)(cfg, cfg['esp32_host'], nominal, record_end, fetched['source'])
        adc_log = owner_adc_log(cfg, inp, record_start - 120., nominal + 1.)
        if inp['mode'] == 'raw':
            doc = raw_document(inp, fetched, cfg, clock_info=clock_info, adc_log=adc_log, now=now)
        else:
            doc = analyse(inp, fetched, cfg, clock_info=clock_info, adc_log=adc_log, now=now)
        try:                          # v3.16/v4.25: per-point mode quality (never fails the analysis)
            q_rows, q_source = owner_quality_rows(inp, proxy_dir, t0 - 1., t1 + 1.)
            doc = attach_quality(doc, inp, q_rows, q_source)
        except Exception as error:    # noqa: BLE001
            doc['mode_quality'] = dict(available=False, reason='mode quality failed: %s' % str(error)[:200])
        doc['runtime_s'] = now() - started
        doc['attempt_by'] = attempt_by
        _atomic_json(analysis_dir(out) / 'result.json', doc)
        summary = doc['summary']
        if doc['mode'] == 'levels':
            short = dict(rms_error_hz=summary['rms_error_hz'], max_abs_error_hz=summary['max_abs_error_hz'],
                         levels_measured=summary['levels_with_settled_point'], levels_total=summary['levels_total'],
                         levels_played=summary['levels_played'])
        else:
            short = None          # raw mode: no residual summary (the outcome keeps its own numbers)
        return write_status(out, state='ready', ref=ref_for(proxy_dir.name), reason=None, finished_unix_s=now(),
                            runtime_s=doc['runtime_s'], source=doc['source'], quality_ok=doc['quality']['ok'],
                            mode=doc['mode'], summary=short,
                            adc_log_threshold=(doc['temporary_settings'] or {}).get('adc_log_threshold'),
                            t0_unix_s=doc.get('t0_unix_s'), warnings=doc['quality']['warnings'],
                            mode_quality={k: (doc.get('mode_quality') or {}).get(k) for k in (
                                'available', 'points_matched', 'points_degraded')})
    except Exception as error:      # noqa: BLE001 - published as 'unavailable' with the reason
        return write_status(out, state='unavailable', ref=None, reason=str(error)[:500], finished_unix_s=now(),
                            runtime_s=now() - started)


def _watchdog(proxy_dir, limit_s):
    """A bounded runtime: past ``limit_s`` the child publishes 'unavailable' and exits (never hangs a pending state)."""
    def fire():
        time.sleep(limit_s)
        try:
            write_status(proxy_dir, state='unavailable', ref=None, reason='analysis exceeded its %.0f s runtime bound' % limit_s,
                         finished_unix_s=time.time())
        finally:
            os._exit(3)
    threading.Thread(target=fire, name='shot-analysis-watchdog', daemon=True).start()


# ------------------------------------------------------------------------------------- card (rule F, one-shot)
def parameters(card, overrides):
    p = {**dict(proxy_job=None, request_id=None, source='auto', republish=True, replace=False, timeout_s=60., mode='auto'),
         **(card.get('parameters') or {}), **(overrides or {})}
    unknown = set(p) - {'proxy_job', 'request_id', 'source', 'republish', 'replace', 'timeout_s', 'mode'}
    if p['mode'] not in ('auto', 'levels', 'raw'):
        raise ValueError('mode must be auto (from the sweep shape), levels or raw')
    if unknown:
        raise ValueError('Unknown analyze.shot_sweep parameter(s): ' + ', '.join(sorted(unknown)))
    for key in ('proxy_job', 'request_id'):
        if p[key] is not None and (not isinstance(p[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', p[key])):
            raise ValueError(key + ' must be a job id / request id string')
    if p['source'] not in ('auto', 'influx', 'vm'):
        raise ValueError('source must be auto, influx or vm')
    for key in ('republish', 'replace'):
        if type(p[key]) is not bool:
            raise ValueError(key + ' must be boolean')
    if type(p['timeout_s']) not in (int, float) or not 5 <= p['timeout_s'] <= 600:
        raise ValueError('timeout_s must be within [5, 600] s')
    return p


def find_proxy(state_root, *, proxy_job=None, request_id=None):
    """A sweep proxy job directory by job id, shot request id (shot_sweep.request_id) or facade request id."""
    jobs = Path(state_root) / 'jobs'
    if proxy_job:
        hits = [p for p in [jobs/proxy_job, *jobs.glob('*/%s' % proxy_job)] if (p / 'job.json').exists()]
        if hits:
            return hits[0]
        raise LookupError('Unknown proxy job ' + proxy_job)
    if request_id:
        candidates = sorted([*jobs.glob('*/progress.json'), *jobs.glob('*/*/progress.json')], key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates[:2000]:
            progress = _read_json(path, {}) or {}
            shot = progress.get('shot_sweep') or {}
            meta = _read_json(path.parent / 'job.json', {}) or {}
            if shot.get('request_id') == request_id or meta.get('sweep_request_id') == request_id:
                return path.parent
        raise LookupError('No sweep proxy job with request id ' + request_id)
    raise ValueError('Give proxy_job or request_id')


def run_card(directory, laser, card, p, *, publish, cancel=lambda: False, emit=lambda *a, **k: None, **_):
    """analyze.shot_sweep: the same procedure on demand (any time, read-only). The result is the card's artifact
    (shot-analysis.json in this job) and, with republish, the proxy's published analysis (unless one is ready and
    replace is false)."""
    directory = Path(directory)
    root = state_root_of(directory)
    proxy = find_proxy(root, proxy_job=p.get('proxy_job'), request_id=p.get('request_id'))
    cfg = load_config(root)
    if cfg is None:
        raise LookupError('post-shot analysis is not configured for this laser (%s missing)' % CONFIG_NAME)
    if p['source'] != 'auto':
        cfg['sources'] = [p['source']]
    cfg['max_runtime_s'] = float(p['timeout_s'])
    publish(dict(state='running', proxy_job=proxy.name))
    existing = read_status(proxy) or {}
    to_proxy = bool(p['republish'] and (p['replace'] or existing.get('state') not in ('ready', 'pending', 'running')))
    out = proxy if to_proxy else directory
    status = run(proxy, cfg=cfg, attempt_by='card:' + directory.name, publish_dir=out, mode=p['mode'])
    doc = read_result(out) or {}
    _atomic_json(directory / 'shot-analysis.json', doc or status)
    emit('shot_analysis', dict(proxy_job=proxy.name, state=status.get('state'), reason=status.get('reason'),
                               published_to_proxy=to_proxy, summary=status.get('summary')))
    publish(dict(state=status.get('state'), proxy_job=proxy.name, published_to_proxy=to_proxy,
                 summary=status.get('summary'), reason=status.get('reason')))
    if status.get('state') != 'ready':
        raise RuntimeError('Shot analysis unavailable: %s' % status.get('reason'))
    return dict(status='succeeded', proxy_job=proxy.name, published_to_proxy=to_proxy, summary=status.get('summary'))


def _report_fatal(proxy_dir):
    """SIGXCPU (the CPU-time bound) and fatal faults are written to status.json / analysis.log, never silent."""
    import faulthandler
    import signal
    faulthandler.enable()                     # a segfault/abort dumps the Python stack into analysis.log

    def on_xcpu(signum, frame):
        try:
            write_status(proxy_dir, state='unavailable', ref=None, finished_unix_s=time.time(),
                         reason='analysis exceeded its CPU-time bound (SIGXCPU)')
        finally:
            os._exit(4)
    signal.signal(signal.SIGXCPU, on_xcpu)


def main(argv=None):
    for key, value in CHILD_ENV.items():      # before numpy is imported anywhere in this process
        os.environ.setdefault(key, value)
    import argparse
    ap = argparse.ArgumentParser(description='ldcon post-shot analysis (one proxy job)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--proxy-dir', required=True)
    r.add_argument('--source', choices=('auto', 'influx', 'vm'), default='auto')
    r.add_argument('--state-root')
    a = ap.parse_args(argv)
    proxy_dir = Path(a.proxy_dir)
    cfg = load_config(Path(a.state_root) if a.state_root else state_root_of(proxy_dir))
    limit = float((cfg or DEFAULTS)['max_runtime_s']) + float((cfg or DEFAULTS)['vm_wait_s']) + 5.
    _report_fatal(proxy_dir)
    _watchdog(proxy_dir, limit)
    if cfg is not None and a.source != 'auto':
        cfg['sources'] = [a.source]
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (int(limit) + 5, int(limit) + 10))
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_AS_BYTES, CHILD_AS_BYTES))
    except (ImportError, ValueError, OSError):
        pass
    status = run(proxy_dir, cfg=cfg) if cfg is not None else write_status(
        proxy_dir, state='unavailable', ref=None, reason='not configured (%s missing)' % CONFIG_NAME)
    print(json.dumps(dict(state=status.get('state'), reason=status.get('reason'), summary=status.get('summary'))))
    return 0 if status.get('state') == 'ready' else 1


if __name__ == '__main__':
    sys.exit(main())
