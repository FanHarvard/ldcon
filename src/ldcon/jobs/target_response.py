"""Finite target-response study through the existing target and history interfaces.

This card uses the parent owner for targets and scoped temporary FF experiments.
It never writes devices, PI gains, limits or learned saved defaults.
External configuration changes/foreground work invalidate its return authority.
"""
import copy
import fcntl
import html
import math
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json, read_json
from ldcon.jobs import lock_target
from ldcon.jobs.target_response_analysis import analyse, C_NM
from ldcon.jobs.study_execution import DEFAULTS as EXECUTION_DEFAULTS

# shift_hz / shift_search / abort_integral_fraction_max: the per-step shift search and promotion (target-ff design
# 2026-10-27, target_response_shift). shift_hz is exclusive with offsets_hz.
DEFAULTS = dict(offsets_hz=None, optimization_goal='settling_criteria', baseline_s=2., observation_s=10.,
    expected_settle_s=3., settling_goal_s=3., ff_max_roundtrips=12, ff_budget_s=1200., evaluation_window_s=1., check_interval_s=.1, confirmation_s=2.,
    mean_error_max_hz=10e6, rms_error_max_hz=10e6, peak_error_max_hz=None,
    minimum_samples=10, max_gap_s=.2, data_wait_s=15., command_timeout_s=90., workflow_parent_id='',
    shift_hz=None, shift_search=None, abort_integral_fraction_max=.95, **EXECUTION_DEFAULTS)


def parameters(card, overrides):
    p = {**copy.deepcopy(DEFAULTS), **card.get('parameters', {}), **overrides}
    if set(p) != set(DEFAULTS):
        raise ValueError('Unknown target-response parameter')
    values = p['offsets_hz']
    if values is not None and (not isinstance(values, list) or not 1 <= len(values) <= 32
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in values)
            or not any(values)):
        raise ValueError('Specify 1..32 finite offsets from the study starting target, including a nonzero offset')
    from ldcon.jobs.study_execution import validate as validate_execution
    validate_execution(p)
    if p['optimization_goal'] not in ('settling_criteria','settling_and_feedforward','settling_feedforward_and_tec'):
        raise ValueError('Unknown target-response optimization goal')
    shift=p['shift_hz']
    if shift is not None:
        if type(shift) not in (int,float) or not math.isfinite(shift) or shift<=0:
            raise ValueError('shift_hz must be a finite positive shift')
        if values is not None:
            raise ValueError('shift_hz and offsets_hz are exclusive')
        if p['optimization_goal']!='settling_criteria':
            raise ValueError('The shift search uses the fixed settling criteria (optimization_goal settling_criteria)')
    from ldcon.jobs.target_response_shift import validate_search
    p['shift_search']=validate_search(p['shift_search']) if shift is not None else p['shift_search']
    if shift is None and p['shift_search'] is not None:
        raise ValueError('shift_search needs shift_hz')
    if type(p['abort_integral_fraction_max']) not in (int,float) or not 0<p['abort_integral_fraction_max']<=1:
        raise ValueError('abort_integral_fraction_max must be in (0, 1]')
    parent=p['workflow_parent_id']
    if not isinstance(parent,str) or (parent and (len(parent)!=32 or any(c not in '0123456789abcdef' for c in parent))):
        raise ValueError('Invalid workflow parent identity')
    for name in set(DEFAULTS)-{'offsets_hz', 'optimization_goal', 'minimum_samples', 'ff_max_roundtrips', 'workflow_parent_id',
                               'shift_hz', 'shift_search', 'abort_integral_fraction_max', *EXECUTION_DEFAULTS}:
        value = p[name]
        if name == 'peak_error_max_hz' and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(name+' must be finite and positive')
    if type(p['ff_max_roundtrips']) is not int or not 1 <= p['ff_max_roundtrips'] <= 12:
        raise ValueError('Feedforward comparison permits at most 12 round trips')
    if p['ff_budget_s'] > 1200 or p['settling_goal_s'] > p['observation_s']:
        raise ValueError('FF budget must be <=1200 seconds and settling goal must fit observation')
    if type(p['minimum_samples']) is not int or not 2 <= p['minimum_samples'] <= 10000:
        raise ValueError('minimum_samples must be 2..10000')
    if not .02 <= p['check_interval_s'] <= p['evaluation_window_s'] <= p['observation_s'] <= 300:
        raise ValueError('Require .02 <= check interval <= evaluation window <= observation <= 300 seconds')
    if not p['evaluation_window_s'] <= p['confirmation_s'] <= p['observation_s']:
        raise ValueError('confirmation_s must cover an evaluation window and fit in the observation')
    if p['max_gap_s'] >= p['evaluation_window_s'] or p['expected_settle_s'] > p['observation_s']:
        raise ValueError('Gap must be shorter than evaluation window; expected settling must fit the observation')
    if p['baseline_s'] > 60 or p['data_wait_s'] > 60 or p['command_timeout_s'] > 300:
        raise ValueError('Baseline/data wait <= 60 seconds and command timeout <= 300 seconds')
    legs=(2*p['shift_search']['max_rounds']+2) if shift is not None else (len(values) if values is not None else 2)+1
    if legs*(p['baseline_s']+p['observation_s']+p['command_timeout_s']+p['data_wait_s']) > 3600:
        raise ValueError('Study worst-case duration exceeds one hour')
    return p


def resolved_parameters(laser, p):
    """Resolve the request limit, never enlarge firmware chunks or owner bounds."""
    result=copy.deepcopy(p)
    if result['offsets_hz'] is None and result.get('shift_hz') is None:
        width=(laser.get('target') or {}).get('max_change_hz')
        if type(width) not in (int,float) or not math.isfinite(width) or width<=0:
            raise ValueError('Automatic jump requires positive target.max_change_hz')
        result['offsets_hz']=[width,0.]
    return result


def validate_laser(laser, p):
    p=resolved_parameters(laser,p)
    if not laser.get('allow_output'):
        raise ValueError('Laser output writes are not enabled')
    if p.get('optimization_goal','settling_criteria')=='settling_feedforward_and_tec':
        from ldcon.owners.ecdl_target_settling import required
        if not required(laser):raise ValueError('TEC/PZT response optimization requires an ECDL owner')
    base = lock_target.resolve_target(laser, {})
    if base is None:
        raise ValueError('A canonical laser target is required')
    document = copy.deepcopy(laser)
    if p.get('shift_hz') is not None:
        # The shift search raises the saved per-step cap itself (trial saves); only the absolute target range and
        # the presence of the cap keys are checked here.
        from ldcon.jobs.target_response_shift import cap_paths
        cap_paths(laser)
        lock_target.check_change(document, base, enforce_step=False)
        return base
    for delta in [*p['offsets_hz'], 0.]:
        target = base+delta
        lock_target.check_change(document, target)
        document['target']['frequency_hz'] = target
    return base


def summary(result):
    """Keep waveform and per-window arrays in artifacts, not polling responses."""
    value = {k:copy.deepcopy(v) for k,v in result.items() if k!='trials'}
    if 'optimization' in value:
        value['optimization'].pop('trials', None)
    if 'shift' in value:
        for row in value['shift'].get('rounds',[]):
            row['trials']=[dict(index=t.get('index'),offset_hz=t.get('offset_hz'),analysis=t.get('analysis'),monitor=t.get('monitor'))
                           for t in row.get('trials',[])]
    for name in ('ff_comparison','ff_search'):
        if name in value:
            for row in value[name].get('rounds',[]):
                row['trials']=[dict(index=t.get('index'),status=t.get('status'),
                    analysis={k:t.get('analysis',{}).get(k) for k in ('status','reason','settling_from_request_s','sample_count')})
                    for t in row.get('trials',[])]
    return value


class Superseded(CardCancelled):
    pass


class StudyContext:
    """CAS binds every write/return to this study's last accepted revision."""
    def __init__(self, directory, laser, api, clock, cancel):
        from ldcon.jobs.operating_point import find_running_lock
        self.directory, self.laser, self.api = Path(directory), laser, api
        self.workflow_parent=None
        invocation=read_json(self.directory/'invocation.json',{})
        parent=(invocation.get('parameters') or {}).get('workflow_parent_id')
        if parent:
            # A parent is exempt from foreground checks only with a matching
            # host-written child receipt, same laser and registered operation.
            self.workflow_parent=parent
            deadline=time.monotonic()+3.
            while time.monotonic()<deadline:
                receipt=read_json(self.directory.parent/parent/'pi-response.json',{})
                if any(c.get('id')==self.directory.name for c in receipt.get('children',[])):break
                if cancel():raise CardCancelled('Response child cancelled before parent confirmation')
                time.sleep(.05)
        self.clock, self.cancel = clock, cancel
        found = find_running_lock(directory, laser)
        if found is None:
            raise DeviceError('Target response study requires an existing running lock; it never starts one')
        self.lock_directory, self.meta, _ = found
        self.lock_id = self.meta['id']
        current = api.get_configuration('lasers', laser['id'])
        self.expected_revision = current['revision']
        self.document = current['document']
        self.pending = False
        self.ff_policy = None
        self.ff_unknown = False
        self.ff_deadline = None
        self.last_check = -math.inf
        self.check(force=True)

    def check(self, *, force=False, ignore_cancel=False):
        if self.ff_deadline is not None and self.clock() >= self.ff_deadline:
            raise CardCancelled('FF comparison deadline reached; no further target requests')
        if not ignore_cancel and self.cancel():
            raise CardCancelled('Target response study cancelled')
        if not force and self.clock()-self.last_check < .5:
            return
        from ldcon.core.operation_registry import OBSERVERS
        jobs = self.api._request('GET', 'api/jobs?active_only=true')['jobs']
        own_laser = [j for j in jobs if (j.get('laser') or {}).get('id') == self.laser['id'] or j.get('set_id') == self.laser['id']]
        if not any(j['id'] == self.lock_id and j['status'] == 'running' for j in own_laser):
            raise Superseded('Parent lock ended or changed; no study restoration')
        if self.workflow_parent and not any(j['id']==self.workflow_parent and j['status']=='running' for j in own_laser):
            raise Superseded('PI-response workflow ended; no new child target commands')
        for j in own_laser:
            if j['id'] in (self.directory.name, self.lock_id):
                continue
            operations = {c.get('operation', c.get('card')) for c in j.get('cards', [])}
            if j['id']==self.workflow_parent and operations=={'tune.pi_response'}:
                record=read_json(self.directory.parent/self.workflow_parent/'pi-response.json',{})
                if any(c.get('id')==self.directory.name and c.get('card')==read_json(self.directory/'invocation.json',{}).get('card_id')
                       for c in record.get('children',[])):
                    continue
                raise Superseded('Workflow parent has not confirmed this response child')
            if not operations or not operations <= (OBSERVERS-{'agent.task','agent.history_review'}):
                raise Superseded('Foreground operation takes priority; no further study writes or restoration')
        for pattern in ('operating-point-requests/pending/*.json','ff-requests/pending/*.json',
                        'runtime-parameter-request-*.json','online-request.json','hot-control-request.json'):
            if any(Path(self.lock_directory).glob(pattern)):
                raise Superseded('Controller adjustment is pending; no further study writes or restoration')
        current = self.api.get_configuration('lasers', self.laser['id'])
        if current['revision'] != self.expected_revision:
            raise Superseded('Laser configuration was superseded; preserve the newer target')
        self.document = current['document']
        self.last_check = self.clock()

    def get_configuration(self, kind, name):
        self.check(force=True)
        result = self.api.get_configuration(kind, name)
        if result['revision'] != self.expected_revision:
            raise Superseded('Concurrent configuration change before target write')
        return result

    def put_configuration(self, kind, name, text, expected_revision, **kwargs):
        self.check(force=True)
        if expected_revision != self.expected_revision:
            raise Superseded('Target write revision differs from study revision')
        self.check(force=True)
        self.pending = True
        # Persist intent BEFORE submitting. Never replay a lost response.
        atomic_json(self.directory/'target-response-command-intent.json', dict(
            expected_revision=expected_revision, submitted_unix_s=time.time()))
        if self.ff_policy is not None:
            result = self.api._request('PUT', 'api/target-response/'+self.directory.name+'/target',
                dict(yaml=text, expected_revision=expected_revision))
        else:
            result = self.api.put_configuration(kind, name, text, expected_revision, **kwargs)
        self.expected_revision = result['revision']
        return result


    def policy_request(self, action, *, sleep, **values):
        """One submitted request, correlated polling only; no mutation replay."""
        if action == 'set': self.check(force=True)
        if self.ff_unknown: raise DeviceError('Previous FF policy result is unconfirmed')
        payload = dict(action=action, **values)
        if action != 'status': payload['expected_revision'] = self.ff_policy['revision']
        self.ff_unknown = True
        queued = self.api._request('POST', 'api/target-response/'+self.directory.name+'/policy', payload)
        deadline = self.clock()+35
        while self.clock() < deadline:
            # Restore is memory-only and remains available during cancellation.
            if action == 'set': self.check(force=True)
            receipt = self.api._request('GET', 'api/target-response/'+self.directory.name+'/policy')
            if receipt.get('request_id') == queued['request_id']:
                if receipt.get('status') != 'completed':
                    raise DeviceError('FF policy request '+receipt.get('status', 'unconfirmed'))
                self.ff_unknown = False
                self.ff_policy = receipt['result']
                return copy.deepcopy(self.ff_policy)
            sleep(.1)
        raise DeviceError('FF policy request timed out; no replay')

    def restore_policy(self, *, sleep):
        if self.ff_policy is None: return 'not_used'
        if self.ff_unknown: return 'unconfirmed_no_replay'
        token=(self.ff_policy.get('session') or {}).get('study_token')
        if not token: return 'defaults_confirmed'
        current=self.policy_request('status', sleep=sleep)
        if (current.get('session') or {}).get('study_token') != token:
            return 'superseded_or_expired_defaults_preserved'
        receipt=self.policy_request('restore', sleep=sleep)
        if receipt.get('active'): raise DeviceError('FF policy restoration not confirmed')
        self.ff_policy = None
        return 'defaults_confirmed'


def write_report(directory, result):
    rows = []
    for leg in result['trials']:
        a = leg.get('analysis', {})
        rows.append('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in (
            leg['offset_hz']/1e6, a.get('status', leg.get('status', 'incomplete')),
            a.get('settling_from_request_s'), a.get('frequency_settling_from_request_s'),
            a.get('pzt_settling_from_request_s'), a.get('thermal',{}).get('overshoot_count'),
            a.get('settling_after_apply_interval_s'),
            a.get('sample_count'), a.get('reason')))+ '</tr>')
    plots = []
    for leg in result['trials']:
        points = leg.get('history', {}).get('samples', [])
        if len(points) < 2:
            continue
        times = [s['time_unix_s']-leg['requested_unix_s'] for s in points]
        errors = [(C_NM/s['wavelength_nm']-leg['target_frequency_hz'])/1e6 for s in points]
        lo, hi = min(errors), max(errors)
        span = max(hi-lo, 1.)
        poly = ' '.join('%.2f,%.2f' % (50+850*(t-times[0])/max(times[-1]-times[0],1e-9),
                           210-170*(e-lo)/span) for t,e in zip(times, errors))
        plots.append('<h2>Offset %.3f MHz</h2><svg viewBox="0 0 940 260" role="img" '
            'aria-label="Full-rate frequency error after target request">'
            '<path d="M50 30 V210 H900" fill="none" stroke="black"/>'
            '<polyline points="%s" fill="none" stroke="#2467aa" stroke-width="1"/>'
            '<text x="50" y="20">Error (MHz): %.4g to %.4g</text>'
            '<text x="50" y="245">Time from request (s): %.3f to %.3f</text></svg>' % (
                leg['offset_hz']/1e6, poly, lo, hi, times[0], times[-1]))
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Target response study</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:1rem}td,th{padding:.5rem;border-bottom:1px solid #ccc}svg{width:100%}</style>'
        '<h1>Target response study</h1><p>'+html.escape(result['status'])+'</p>'
        '<p>All recorded new wavelengths, without averaging or decimation. Database arrival delay is excluded. '
        'Command application is bounded by host request/confirmation times, not independently timestamped. '
        'ESP32/host clock alignment must be checked before interpreting subsecond physical latency. '
        'RMS is error relative to target; standard deviation removes the mean error. '
        'ECDL completion includes the actual PZT returning to its frozen center. DAC uses receive time; optical data uses source time. '
        'PZT overshoot is the excursion past the center opposite the initial FF direction, with a fixed noise floor. '
        'Proposed profiles are not installed automatically.</p><table><tr><th>Offset (MHz)</th><th>Result</th>'
        '<th>Joint settling from request (s)</th><th>Frequency settling (s)</th><th>PZT settling (s)</th>'
        '<th>PZT overshoot (count)</th><th>After-application interval (s)</th><th>Samples</th><th>Reason</th></tr>'
        +''.join(rows)+'</table>'+''.join(plots))
    optimization = result.get('optimization')
    if optimization:
        import json
        page += '<h2>Agent evaluation</h2><p>'+html.escape(optimization['status'])+'</p>'
        for key in ('reason','limitations','validation'):
            if optimization.get(key):page += '<p>'+html.escape(optimization[key])+'</p>'
        page += '<pre>'+html.escape(json.dumps(optimization.get('parameters',{}),indent=2))+'</pre>'
        if optimization.get('feedforward'):
            page+='<h3>Target feedforward and internal step candidate</h3><pre>'+html.escape(json.dumps(optimization['feedforward'],indent=2))+'</pre>'
        page += '<p>Candidate only. No saved defaults, lock criteria, PI or FF parameters were changed.</p>'
    if result.get('ff_comparison'):
        import json
        page += '<h2>Physical FF comparison</h2><pre>'+html.escape(json.dumps(summary(result)['ff_comparison'],indent=2))+'</pre>'
        page += '<p>Temporary policy restoration: '+html.escape(result.get('ff_restoration','pending'))+'</p>'
    if result.get('shift'):
        import json
        block=result['shift']
        page += ('<h2>Per-step shift search</h2><p>Requested %.1f MHz (effective %.1f MHz); largest shift passing in both directions: %s; '
                 'adopted: %s; adoption: %s</p>' % (block['requested_hz']/1e6, block['effective_requested_hz']/1e6,
                 'none' if block.get('best_passing_hz') is None else '%.1f MHz' % (block['best_passing_hz']/1e6),
                 'none' if block.get('adopted_hz') is None else '%.1f MHz' % (block['adopted_hz']/1e6),
                 html.escape(str((block.get('adoption') or {}).get('status')))))
        page += '<table><tr><th>Round</th><th>Shift (MHz)</th><th>Verdict</th><th>Worst settling (s)</th><th>Reasons</th><th>Quality (report only)</th></tr>'
        for row in block.get('rounds',[]):
            page += '<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in (row.get('index'), row['shift_hz']/1e6, row.get('verdict'),
                row.get('worst_settle_s'), '; '.join(row.get('reasons',[])), '; '.join(row.get('quality',[]))))+'</tr>'
        page += '</table><pre>'+html.escape(json.dumps(dict(adoption=block.get('adoption'),restore=block.get('restore'),audit=block.get('audit')),indent=2))+'</pre>'
    page += '</html>'
    (Path(directory)/'target-response.html').write_text(page)



from ldcon.jobs.response_record import history_tail_complete

def run_card(directory, laser, card, p, *, clock, sleep, cancel, emit,
             publish=lambda _: None, api=None, history=None, wall_clock=time.time, target_runner=None,
             decision_provider=None, resident_selection=None, control_reader=None):
    from ldcon.jobs.dashboard_api import Dashboard, state_dir_of
    from ldcon.jobs.wavelength_history import configured_reader
    p = parameters({'parameters':p}, {})
    validate_laser(laser, p)
    directory = Path(directory)
    api = api or Dashboard.from_job_directory(directory)
    history = history or configured_reader(state_dir_of(directory))
    target_runner = target_runner or lock_target.run_lock_target
    result = dict(schema='ldcon.target-response/2', status='running', laser_id=laser['id'], parameters=p,
                  trials=[], restoration='not_needed', proposed_profile=None)
    # Per-laser study exclusion only; manual target/sweep operations remain free
    # to preempt and are detected by revision and active-job checks.
    guard = (directory.parent/'target-response.lock').open('a')
    ctx = None
    child = None
    measurement_finished = False
    try:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeviceError('Another target-response study is active') from None
        selected = None
        profile = None
        if p['execution_mode']=='agent':
            from ldcon.jobs.study_execution import resident, decision_parent
            delegated=bool(p['execution_profile'] or card.get('agent_execution'))
            selected = resident_selection or (decision_parent if delegated else resident)(directory, p['agent_card_id'])
            if delegated:
                from ldcon.agents.agent_profiles import select_task
                from ldcon.web.agent_task_api import admission_path
                store,meta=selected[:2]
                admission=read_json(admission_path(store.root.parent,meta['id']),{})
                if admission:
                    if admission.get('parameters')!=p or admission.get('card_id')!=card['id']:
                        raise ValueError('Study admission differs from frozen invocation')
                    profile=admission['profile']
                else:profile,_=select_task(store.root.parent,card,p,meta=meta)
        if selected and profile and decision_provider is None:
            from ldcon.agents.task_decisions import TaskDecisions
            child=TaskDecisions(directory,selected,profile,clock=clock,cancel=cancel,
                deadline=clock()+min(3600.,profile['timeout_s']))
            child.connect()
            decision_provider=child
        ctx = StudyContext(directory, laser, api, clock, cancel)
        result['requested_parameters']=copy.deepcopy(p)
        p=resolved_parameters(ctx.document,p)
        result['parameters']=copy.deepcopy(p)
        base = validate_laser(ctx.document, p)
        result.update(starting_target_hz=base, starting_revision=ctx.expected_revision, parent_lock_id=ctx.lock_id)
        from ldcon.jobs import target_response_feedforward as ff
        try:result['feedforward_context']=ff.configuration(ctx.document)
        except (ValueError,KeyError,TypeError):result['feedforward_context']=dict(status='unavailable')
        control_reader=control_reader or ff.capture

        def wait(duration, *, restoring=False, tick=None):
            end = clock()+duration
            while clock() < end:
                ctx.check(ignore_cancel=restoring)
                if tick is not None:tick()
                sleep(max(0.,min(.1, end-clock())))

        def move(target, *, restoring=False):
            ctx.check(force=True, ignore_cancel=restoring)
            # A restoration may ignore the stop request only after obtaining a
            # fresh unchanged revision. Foreground supersession is never ignored.
            old_cancel = ctx.cancel
            if restoring:
                ctx.cancel = lambda: False
            try:
                def checked_cancel():
                    ctx.check()
                    return False
                timeout=p['command_timeout_s']
                if ctx.ff_deadline is not None:
                    remaining=ctx.ff_deadline-clock()
                    reserve=(p['command_timeout_s']+p['observation_s']+p['data_wait_s']+80
                        if target != base else 0)
                    timeout=min(timeout,remaining-reserve)
                    if timeout<1: raise CardCancelled('FF return budget exhausted before target submission')
                command = lock_target.validate(dict(target_frequency_hz=target, verify_samples=1,
                    tolerance_hz=None, sample_interval_s=.1, timeout_s=timeout))
                reply = target_runner(directory, copy.deepcopy(ctx.document), card, command,
                    clock=clock, sleep=sleep, cancel=checked_cancel, emit=emit, api=ctx)
                if not reply.get('locked'):
                    raise DeviceError('Target was saved without an active lock; study result is unavailable')
                ctx.pending = False
                ctx.check(force=True)
                return reply
            finally:
                ctx.cancel = old_cancel

        # Verify that the configured history is readable before any target write.
        initial = history.read(wall_clock()-p['baseline_s'], wall_clock())
        if initial.get('integrity_errors') or len(initial['samples']) < 2:
            raise DeviceError('Recent wavelength history is missing or inconsistent; no target was changed')
        from ldcon.jobs import target_response_thermal as thermal
        from ldcon.owners.ecdl_target_settling import required as thermal_required
        shift_mode=p.get('shift_hz') is not None
        # The shift search judges frequency arrival only (completion rule 2026-10-27); the PZT/TEC evidence of the
        # ECDL is recorded with every leg (control_before/after) and reported, never a joint settling criterion.
        pzt_required=thermal_required(ctx.document) and not shift_mode
        if pzt_required:
            # Freeze against owner BASE policy before any candidate overlay or write.
            control=control_reader(ctx.document,result['feedforward_context'])
            context=(control.get('thermal') or {}).get('centering',{})
            slope=result['feedforward_context']['wavelength_nm_per_count']
            jump=max(p['offsets_hz'],key=abs)
            reference=(C_NM/(base+jump)-C_NM/base)/slope*result['feedforward_context']['target_feedforward_scale']
            result['pzt_criteria']=thermal.criteria(context,initial['samples'],
                reference_ff_step_count=reference,confirmation_s=max(p['confirmation_s'],context.get('confirmation_s',0.)))
            result['thermal_context']=copy.deepcopy(control['thermal'])
        def measure_round(round_index=0, offsets=None, *, joint_pzt=True, monitor_factory=None):
            """One ordered set of legs (the configured offsets, or the shift search's [+S, 0]); ``joint_pzt`` False
            judges frequency arrival only (shift search); ``monitor_factory`` samples the lock's live row per leg."""
            offsets=list(offsets) if offsets is not None else list(p['offsets_hz'] or [])
            if not offsets:raise ValueError('No target offsets to measure')
            if offsets[-1] != 0:offsets.append(0.)
            joint=pzt_required and joint_pzt
            first=len(result['trials'])
            for index, offset in enumerate(offsets):
                ctx.check(force=True)
                wait(p['baseline_s'])
                monitor=monitor_factory() if monitor_factory is not None else None
                tick=monitor.sample if monitor is not None else None
                control_before=control_reader(ctx.document,result['feedforward_context'])
                if joint:
                    thermal_state=control_before.get('thermal') or {}
                    center=thermal_state.get('centering',{})
                    if (control_before.get('status')!='observed' or not center.get('reference_confirmed')
                            or center.get('reference_revision')!=result['pzt_criteria']['reference_revision']):
                        raise Superseded('PZT center or base policy changed before the target study')
                    period=thermal_state['parameters']['supervision_interval_s']
                t0 = wall_clock()
                leg = dict(index=len(result['trials']), round_index=round_index, offset_hz=offset, target_frequency_hz=base+offset,
                           previous_target_frequency_hz=ctx.document['target']['frequency_hz'],
                           requested_unix_s=t0, status='requesting',control_before=control_before)
                if joint:
                    delta_nm=C_NM/(base+offset)-C_NM/leg['previous_target_frequency_hz']
                    if not delta_nm:raise ValueError('Thermal response requires a nonzero target transition')
                    leg['pzt_evaluation']=dict(criteria=copy.deepcopy(result['pzt_criteria']),
                        direction=1 if delta_nm/result['feedforward_context']['wavelength_nm_per_count']>0 else -1,
                        period_s=period)
                result['trials'].append(leg)
                atomic_json(directory/'target-response.json', result)
                publish(dict(state='changing_target', index=index, offset_hz=offset))
                leg['command'] = move(base+offset)
                confirmed = wall_clock()
                leg['confirmed_unix_s'] = confirmed
                if tick is not None:tick()
                publish(dict(state='observing', index=index))
                observation=p['observation_s']
                if joint:
                    observation=max(observation,2*period+result['pzt_criteria']['confirmation_s']-(confirmed-t0))
                wait(observation,tick=tick)
                end = wall_clock()
                leg['record_end_unix_s'] = end
                leg['control_after']=control_reader(ctx.document,result['feedforward_context'])
                if ctx.ff_policy is not None:
                    policy=ctx.policy_request('status',sleep=sleep)
                    leg['owner_target_move']=copy.deepcopy(policy.get('last_move',{}))
                from ldcon.jobs.response_record import acquire as acquire_response_record
                captured=acquire_response_record(history,t0-p['baseline_s'],t0,end,
                    data_wait_s=p['data_wait_s'],max_gap_s=p['max_gap_s'],clock=clock,
                    wait=wait,check=lambda:ctx.check(force=True))
                leg['data_attempts']=captured['attempts']
                leg['history']=captured['history']
                if monitor is not None:leg['monitor']=copy.deepcopy(monitor.record)
                if joint:
                    after=(leg['control_after'].get('thermal') or {}).get('centering',{})
                    if after.get('reference_revision')!=result['pzt_criteria']['reference_revision']:
                        leg['history'].setdefault('integrity_errors',[]).append('PZT reference or base policy changed')
                leg['analysis'] = thermal.evaluate_leg(leg,p)
                delta = leg['target_frequency_hz']-leg['previous_target_frequency_hz']
                errors = [C_NM/s['wavelength_nm']-leg['target_frequency_hz'] for s in leg['history']['samples']
                          if t0 <= s['time_unix_s'] <= end]
                leg['analysis']['overshoot_hz'] = (max(0., max(math.copysign(1.,delta)*v for v in errors))
                                                   if delta and errors else None)
                leg['status'] = 'completed'
                emit('target_response_trial', dict(index=index, **{k:v for k,v in leg['analysis'].items() if k!='windows'}))
                atomic_json(directory/'target-response.json', result)
            return result['trials'][first:]

        if shift_mode:
            # target-ff design (2026-10-27): the per-step shift search (trial caps, judge, promotion / restoration)
            from ldcon.jobs import target_response_shift as shift
            shift.run(directory, result, ctx, p, measure_round=measure_round, clock=clock, sleep=sleep,
                      publish=publish, emit=emit, wall_clock=wall_clock)
        elif selected and p['optimization_goal'] in ('settling_and_feedforward','settling_feedforward_and_tec'):
            if p['optimization_goal']=='settling_feedforward_and_tec':
                from ldcon.jobs.target_response_thermal_experiment import run as run_experiment
            else:
                from ldcon.jobs.target_response_experiment import run as run_experiment
            result['ff_comparison'] = run_experiment(directory, result, ctx, selected,
                measure_round=measure_round, clock=clock, sleep=sleep, cancel=cancel,
                publish=publish, provider=decision_provider)
            result['ff_restoration'] = ctx.restore_policy(sleep=sleep)
            ctx.ff_deadline = None
        else:
            measure_round()
        ctx.check(force=True)
        result['restoration'] = 'starting_target_confirmed'
        successful = [x['analysis']['settling_from_request_s'] for x in result['trials'] if x['analysis']['status']=='settled']
        if shift_mode:
            # Failing rounds are expected evidence of a search; the study succeeded when at least one round passed
            # and the search ended normally (the adoption block says what was adopted or restored).
            result['status'] = 'succeeded' if result['shift'].get('best_passing_hz') is not None else 'inconclusive'
        else:
            result['status'] = 'succeeded' if len(successful)==len(result['trials']) else 'inconclusive'
        if successful:
            result['proposed_profile'] = dict(status='candidate_only', applied=False,
                scope='request-to-settling under recorded configuration, steps and source cadence',
                expected_settle_s=max(successful)+p['check_interval_s'],
                all_trials_settled=len(successful)==len(result['trials']),
                criteria={k:p[k] for k in ('evaluation_window_s','check_interval_s','confirmation_s',
                    'mean_error_max_hz','rms_error_max_hz','peak_error_max_hz','minimum_samples','max_gap_s')})
        # Physical phase is finished. Persist plots before waiting for a model.
        # A later target change must never provoke restoration by this analysis.
        measurement_finished = True
        result['final_configuration_revision']=ctx.expected_revision
        result['feedforward_identification']=ff.identify(result)
        if result.get('ff_comparison',{}).get('action')=='inconclusive': result['status']='inconclusive'
        result['measurement_status'] = result['status']
        atomic_json(directory/'target-response.json', result)
        write_report(directory, result)
        if selected:
            from ldcon.jobs.target_response_decisions import optimize
            try:
                result['optimization'] = optimize(directory, result, selected, clock=clock, sleep=sleep,
                    cancel=cancel, publish=publish, provider=decision_provider)
                if result['optimization']['status'] != 'candidate_only':result['status']='inconclusive'
                if result.get('ff_comparison',{}).get('action')=='select':
                    result['optimization']['validated_physical_candidate']=copy.deepcopy(result['ff_comparison']['parameters'])
            except Exception as analysis_error:
                result['optimization'] = dict(status='cancelled' if isinstance(analysis_error,CardCancelled) else 'failed',
                    error_type=type(analysis_error).__name__, applied=False,
                    reason='Agent evaluation did not produce a validated candidate; original measurements and plots retained')
                result['status'] = 'cancelled' if isinstance(analysis_error,CardCancelled) else 'inconclusive'
        if selected and result.get('optimization',{}).get('status')=='candidate_only':
            from ldcon.jobs.study_adoption import submit
            result['adoption']=submit(directory,result,api)
        return result
    except (Exception, KeyboardInterrupt) as error:
        result.update(status='cancelled' if isinstance(error,CardCancelled) else 'failed',
                      error_type=type(error).__name__)
        result['restoration'] = 'not_attempted'
        if ctx is not None and result.get('shift') is not None and not measurement_finished:
            from ldcon.jobs.target_response_shift import finalize as finalize_shift
            try:finalize_shift(ctx,result,reason=type(error).__name__)
            except Exception as ceiling_error:
                result['shift']['restore']=dict(status='left_for_readiness',reason='%s: %s'%(type(ceiling_error).__name__,ceiling_error))
        if ctx is not None and ctx.ff_policy is not None:
            result['target_confirmation'] = dict(canonical_target_hz=ctx.document.get('target',{}).get('frequency_hz'),
                owner_actual_target='unverified_after_interruption',
                note='A saved canonical target may precede or differ from owner application; no restoration claimed')
        if ctx is not None and not measurement_finished:
            if isinstance(error, CardCancelled) and ctx.ff_policy is not None:
                result['restoration'] = 'skipped_cancelled_scoped_experiment_no_further_target_write'
            elif isinstance(error, Superseded):
                result['restoration'] = 'skipped_newer_foreground_or_configuration'
            elif ctx.pending:
                result['restoration'] = 'skipped_unconfirmed_command_no_replay'
            else:
                try:
                    ctx.check(force=True, ignore_cancel=True)
                    if ctx.document['target']['frequency_hz'] != result['starting_target_hz']:
                        move(result['starting_target_hz'], restoring=True)
                    result['restoration'] = 'starting_target_confirmed'
                except Exception as restore_error:
                    result['restoration'] = 'unverified_'+type(restore_error).__name__
        raise
    finally:
        try:
            if ctx is not None and ctx.ff_policy is not None:
                try: result['ff_restoration'] = ctx.restore_policy(sleep=sleep)
                except Exception as cleanup_error:
                    result['ff_restoration'] = 'unconfirmed_'+type(cleanup_error).__name__
            if child:
                result['explanation']=child.explain(summary(result))
                from ldcon.agents.agent_task import _parent_result
                _parent_result(selected[0],selected[1],p,directory,{**result,'report_author':'task_model'})
            atomic_json(directory/'target-response.json', result)
            write_report(directory, result)
        finally:
            if child:child.close()
            guard.close()
