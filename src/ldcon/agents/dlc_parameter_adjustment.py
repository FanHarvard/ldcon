"""One task-model workflow over owner-confirmed DLC adjustment families.

The monitor only starts this task. Selection, paired measurements, confirmation
and adoption are identical for a manual invocation. Models have evidence access,
not raw controller credentials or direct physical commands.
"""
from copy import deepcopy
import math
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.interface_schema import load_mapping
from ldcon.core.records import read_json, atomic_json
from ldcon.jobs.change_measure import ChangeMeasure
from ldcon.jobs.dlc_parameter_measurement import ParameterAdapter, candidate
from ldcon.jobs.study_execution import export_evidence, parse_proposal


def evidence(directory, result, state, turn):
    prefix = 'parameters-%03d-' % turn
    docs = [(prefix+'summary', deepcopy(result)), (prefix+'owner', deepcopy(state))]
    for row in result['measurements'][-3:]:
        raw = read_json(directory/row['path'])
        for name, values, size in (('samples', raw['samples'], 120),
                                  ('controls', raw.get('control_observations', []), 12)):
            for i in range(0, len(values), size):
                docs.append((prefix+row['id']+'-'+name+'-%05d'%i, values[i:i+size]))
    return docs


def response(answer, *, selection=False):
    value = parse_proposal(answer, allow_progress_prefix=True)
    if not isinstance(value, dict) or set(value) != {'action', 'parameters'}:
        raise ValueError('Return exactly action and parameters')
    action, args = value['action'], value['parameters']
    expected = ({'choose': {'family', 'reason'}, 'finish': {'reason'}} if selection else
        {'trial': {'candidate', 'reason'}, 'adopt': {'trial_id', 'reason'}, 'finish': {'reason'},
         'observe': {'wait_s', 'duration_s', 'reason'}})
    if (action not in expected or not isinstance(args, dict) or set(args) != expected[action]
            or not isinstance(args['reason'], str) or not args['reason'].strip()):
        raise ValueError('Unsupported decision or missing reason')
    return value


def _run(directory, p, *, clock, sleep, cancel, emit, selected, decision_session,
        adapter=None, report_writer=None):
    directory = Path(directory)
    frozen = read_json(directory/'invocation.json').get('adjustment_plan') or {}
    if frozen.get('kind') != 'dlc_adjustment':raise ValueError('Frozen DLC adjustment plan required')
    laser = load_mapping((directory/'laser.yaml').read_text())
    adapter = adapter or ParameterAdapter(laser, directory.name, clock=clock)
    if hasattr(adapter, 'supervise'):adapter.supervise(directory, p, laser)
    result = dict(status='running', family=None, trials=[], measurements=[], adopted=False,
        physical_outcome='no_owner_command_in_flight', target_frequency_hz=adapter.target,
        interpretation='Finite paired comparisons; no global optimum or long-term stability claim')
    adopted = False

    def save():atomic_json(directory/'parameter-adjustment.json', result)
    def check():
        if cancel() or clock() >= decision_session.deadline:raise CardCancelled('Adjustment cancelled/deadline')
        adapter.check()
    def request(prompt, *, selecting=False):
        for attempt in range(3):
            check()
            store, _, _, _ = selected
            _, manifest = export_evidence(directory, store, laser['id'],
                evidence(directory, result, adapter.state, decision_session.turns))
            mode = getattr(adapter, 'operating_mode', None)
            if mode and mode.get('mode') in ('measurement', 'characterization'):
                from ldcon.agents.operating_mode_client import instructions
                policy = instructions(mode)
            else:policy = ''
            raw = decision_session.request(dict(kind='dlc_adjustment_decision', prompt=prompt+policy+
                '\nTask objective: '+p['prompt']+' '+p['adjustment_objective'], evidence_files=manifest), tick=check)
            try:
                value = response(raw, selection=selecting)
                action, args = value['action'], value['parameters']
                if action == 'choose' and args['family'] not in supported:
                    raise ValueError('Family is not admitted and supported by this owner')
                if action == 'trial':candidate(result['family'], args['candidate'])
                if action == 'observe':
                    if (any(type(args[k]) not in (int,float) or not math.isfinite(args[k]) for k in ('wait_s','duration_s'))
                            or not 0 <= args['wait_s'] <= 86400 or not 0 < args['duration_s'] <= 86400):
                        raise ValueError('Observation timing must be finite and positive')
                    if args['wait_s']+args['duration_s']+30 >= decision_session.deadline-clock():
                        raise ValueError('Observation exceeds the remaining finite task budget')
                if action == 'adopt':
                    i = args['trial_id']
                    if type(i) is not int or not 0 <= i < len(result['trials']):raise ValueError('Unknown trial')
                    if not result['trials'][i]['eligible']:raise ValueError('Incomplete comparison evidence')
                    if p['adoption_mode'] != 'auto_within_approved':raise ValueError('This run is report-only')
            except (ValueError, TypeError) as error:
                result['last_rejection'] = str(error);save()
                if attempt == 2:raise ValueError('Three invalid decisions; no command replay') from error
                continue
            result.pop('last_rejection', None)
            atomic_json(directory/('parameter-decision-%03d.json'%decision_session.turns), value)
            return value

    check()
    supported = [f for f in frozen['measurements']
        if adapter.state.get('parameter_capabilities', {}).get(f, {}).get('supported') is True
        and (getattr(adapter,'monitor_families',None) is None or f in adapter.monitor_families)]
    family = frozen['family']
    if family == 'auto':
        answer = request('Select one useful adjustment family from the admitted owner capabilities, or finish '
            'without physical changes if none is justified. Review current state, supplied monitor events, recent '
            'source history and prior adjustment outcomes using the advertised read-only evidence tools. '
            'PI compares independent P/I/integral contribution; current compares the diode current offset; slow_loop '
            'compares TEC centering gain/period/PZT center; feedforward compares the internal current FF coefficient. '
            'A quality warning is a prompt to assess, not proof that a particular knob must move. Return '
            '{"action":"choose","parameters":{"family":"...","reason":"..."}} or '
            '{"action":"finish","parameters":{"reason":"..."}}. Supported families: '+str(supported), selecting=True)
        if answer['action'] == 'finish':
            result.update(status='succeeded', reason=answer['parameters']['reason'],
                message='Assessment completed without a physical trial', output_writes=0)
            save();return result
        family = answer['parameters']['family']
        result['selection_reason'] = answer['parameters']['reason']
    if family not in supported:raise ValueError('Requested family has no admitted owner capability')
    result['family'] = family
    adapter.select_family(family)
    if family == 'pi':
        from ldcon.agents.dlc_pi_adjustment import run as pi_run
        outcome = pi_run(directory, p, clock=clock, sleep=sleep, cancel=cancel, emit=emit,
            selected=selected, decision_session=decision_session, adapter=adapter, report_writer=report_writer,
            plan=dict(kind='dlc_pi_change_measure', measurement=frozen['measurements']['pi']))
        result.update(status=outcome['status'],adopted=outcome['adopted'],pi_result_path='pi-adjustment.json')
        save();return dict(outcome,family='pi')
    measurement = deepcopy(frozen['measurements'][family])
    result['checkpoint'] = deepcopy(adapter.origin)
    save()

    def measure(values, role, *, restore=False):
        check()
        if restore:
            receipt = adapter.restore_checkpoint()
            result.setdefault('restoration_receipts', []).append(receipt)
        q = deepcopy(measurement)
        q['target_frequency_hz'] = adapter.target
        q['changes'] = [] if restore or role == 'baseline' else [dict(parameter=k, value=v) for k,v in values.items()]
        remaining = decision_session.deadline-clock()
        if remaining < q['baseline_s']+q['wait_s']+q['duration_s']+10:
            raise CardCancelled('Insufficient task time for a complete comparison window')
        q['timeout_s'] = min(q['timeout_s'], remaining)
        name = 'parameter-window-%03d'%len(result['measurements'])
        runner = ChangeMeasure(adapter, q, clock=clock, wall_clock=adapter.wall_clock,
            sleep=sleep, cancel=cancel, emit=emit)
        try:raw = runner.run()
        finally:atomic_json(directory/(name+'.json'), runner.result)
        if raw['status'] != 'succeeded':raise DeviceError('Incomplete measurement window; no adoption')
        row = dict(id=name, role=role, settings=deepcopy(values), summary=raw['summary'], path=name+'.json')
        result['measurements'].append(row);save();emit('parameter_measurement_completed', row)
        return row

    try:
        adapter.begin()
        result['checkpoint']=deepcopy(adapter.origin);save()
        baseline = measure(adapter.origin, 'baseline')
        while decision_session.turns < decision_session.profile['max_turns']-2:
            check()
            if decision_session.deadline-clock() < 30:break
            answer = request('Choose a bounded informative candidate, request confirmation/adoption of a recorded '
                'trial, or finish. Compare full time-stamped wavelength/optical measurements, sustained bias, '
                'oscillation, drift, PZT/TEC coupling and ordinary scatter. Current is the DLC current OFFSET '
                'in mA; total current including internal FF is checked independently by the owner. Slow-loop '
                'parameters set TEC centering behavior, not a direct target change. A restored TEC command is '
                'not proof of thermal settling: use the recorded wait/window and say if it is inadequate. '
                'An FF candidate is justified only if PZT motion in the evidence identifies its effect; unchanged '
                'PZT is not a coefficient calibration. Choose a small initial experiment from evidence, rather '
                'than jumping across physical bounds. Assess metric trade-offs jointly and retain practical '
                'headroom for drift; do not optimize to a threshold or require every metric to improve. '
                'The host returns to the trial checkpoint for matched baseline. First adopt requests a repeated '
                'candidate plus baseline; review that confirmation and issue adopt on its new trial_id to commit. '
                'No target, safety, calibration, firmware, new hardware sweep or optical-threshold edits are allowed. '
                'Foreground operations have priority. Return {"action":"trial","parameters":{"candidate":{...},'
                '"reason":"..."}}, {"action":"adopt","parameters":{"trial_id":0,"reason":"..."}}, '
                'or {"action":"finish","parameters":{"reason":"..."}}. To change an inadequate measurement '
                'window, return {"action":"observe","parameters":{"wait_s":5,"duration_s":60,"reason":"..."}} '
                '(values are illustrative): this creates a fresh checkpoint baseline and invalidates old adoption '
                'eligibility, preserving comparable windows. Explain actual gains, costs and limits.')
            action, args = answer['action'], answer['parameters']
            if action == 'finish':result['reason'] = args['reason'];break
            if action == 'observe':
                measurement.update(wait_s=args['wait_s'],duration_s=args['duration_s'])
                measurement['timeout_s'] = measurement['baseline_s']+measurement['wait_s']+measurement['duration_s']+30
                for previous in result['trials']:
                    previous.update(eligible=False, ineligible_reason='Measurement duration changed; a fresh comparison is required')
                result.setdefault('measurement_plan_changes',[]).append(deepcopy(args))
                baseline = measure(adapter.origin, 'baseline', restore=True)
                continue
            prior = result['trials'][args['trial_id']] if action == 'adopt' else None
            values = args['candidate'] if action == 'trial' else prior['candidate']
            if prior and 'confirmation_of' in prior:
                check()
                proposal = adapter.preflight([dict(parameter=k, value=v) for k,v in values.items()],
                    measurement['apply_mode'], measurement['measurements'])
                result['adoption_receipt'] = adapter.apply(proposal, lambda:None)
                receipt = adapter.close(adopt=True)
                result['cleanup'] = receipt
                if (not receipt or receipt.get('state') != 'adopted' or receipt.get('confirmed') is not True
                        or receipt.get('settings') != values or receipt.get('identity') != adapter.identity
                        or receipt.get('target_restored') is not True):
                    raise CardCancelled('Adoption preempted or unconfirmed')
                adopted = True
                result.update(adopted=True, adopted_settings=deepcopy(values), reason=args['reason'])
                break
            if decision_session.deadline-clock() < 2*measurement['timeout_s']+30:
                result['last_rejection'] = 'Insufficient time for another complete pair; finish or adopt a confirmed candidate'
                save();continue
            trial = measure(values, 'candidate' if action == 'trial' else 'confirmation')
            after = measure(adapter.origin, 'matched_baseline', restore=True)
            row = dict(id=len(result['trials']), candidate=deepcopy(values), before=baseline['id'],
                trial=trial['id'], after=after['id'], eligible=True, reason=args['reason'])
            if prior:row['confirmation_of'] = prior['id']
            result['trials'].append(row);baseline = after;save()
        result['status'] = 'succeeded'
        result.setdefault('reason', 'Finite decision budget reached; checkpoint retained')
    finally:
        try:
            if not adopted and not result.get('cleanup'):result['cleanup'] = adapter.close(adopt=False)
            receipt = result.get('cleanup') or {}
            if result['status'] == 'succeeded' and not adopted:
                if (receipt.get('confirmed') is not True or receipt.get('identity') != adapter.identity
                        or receipt.get('state') != 'restored' or receipt.get('settings') != adapter.origin):
                    raise DeviceError('Checkpoint restoration is unconfirmed')
                if receipt.get('target_restored') is not True or receipt.get('reason','completed') != 'completed':
                    result.update(status='cancelled', reason='Foreground or owner preemption; checkpoint command restoration confirmed')
        except Exception as error:
            result.update(status='unknown', physical_outcome='requires_inspection', cleanup_error=type(error).__name__)
            atomic_json(directory/'device-review.json', dict(source='dlc_parameter',
                message='Parameter cleanup unconfirmed; inspect owner, do not replay'))
            save();raise
        if result['status'] == 'running':result['status'] = 'interrupted'
        save()
        try:
            from ldcon.jobs.measurement_report import write_report
            frames = [read_json(directory/row['path']) for row in result['measurements']]
            report = (report_writer or write_report)(directory, dict(schema='ldcon.parameter-comparison/1',
                status=result['status'], steps=frames, parameter_result=result))
            import shutil
            shutil.copyfile(report, directory/'report.pdf');result['report'] = 'report.pdf'
        except Exception as error:result['report_error'] = type(error).__name__
        save()
    result['message'] = ('Repeated measured candidate adopted for this lock' if adopted else
        'Comparison completed; checkpoint retained' if result['status']=='succeeded' else
        'Adjustment interrupted; inspect the recorded owner receipt')
    return result


def run(directory, p, *, clock, sleep, cancel, emit, selected, decision_session,
        adapter=None, report_writer=None):
    laser=load_mapping((Path(directory)/'laser.yaml').read_text())
    adapter=adapter or ParameterAdapter(laser,Path(directory).name,clock=clock)
    try:
        return _run(directory,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,
            selected=selected,decision_session=decision_session,adapter=adapter,report_writer=report_writer)
    except Exception as error:
        if isinstance(error,CardCancelled) or cancel():raise
        if (getattr(adapter,'uncertain',False) or getattr(adapter,'begin_pending',False)
                or not hasattr(adapter,'session')):raise
        try:proof=adapter.session.rpc('pi_trial_receipt')
        except Exception:raise error
        trial=proof.get('trial') or {};last=trial.get('last') or {}
        known=(not proof.get('fault') and trial.get('active') is None and
            (not getattr(adapter,'begin_confirmed',False) or
             last.get('identity')==adapter.identity and last.get('confirmed') is True and last.get('state')=='restored'))
        if not known:raise
        from ldcon.agents.resident_adjustment import HostProtocolStop
        atomic_json(Path(directory)/'adjustment-known-stop.json',dict(reason=type(error).__name__,
            owner_receipt=proof,physical_outcome='no_owner_command_in_flight'))
        raise HostProtocolStop(str(error),dict(status=dict(state='restored' if last else 'not_started',active=False),
            latest_result=dict(state='restored' if last else 'not_started'))) from error
