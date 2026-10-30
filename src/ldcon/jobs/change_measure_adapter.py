"""Change/measure bindings to the existing owner-leased operating-point ports.

No new device connection is made for a locked action. Multi-axis coordination is
explicitly unavailable here: the old host-interleaved schedule is not an atomic
or owner-coordinated transaction. Typed owner bindings can supply these modes.
"""
import copy
from dataclasses import asdict
import time
from pathlib import Path
import uuid

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json
from ldcon.jobs import change_measure
from ldcon.jobs.operating_point import (OperatingPointMove, OperatingPointQueue, axis_map,
                                       find_running_lock, open_unlocked_ports, validate)


class MeasurementMove(OperatingPointMove):
    def observation(self):
        # Selected measurements are captured by ChangeMeasure. Owner-side
        # physical monitoring remains active independently of these switches.
        return None


class OperatingPointMeasurementAdapter:
    def __init__(self, movement, laser, *, clock, sleep, cancel, emit, wall_clock=time.time,
                 transaction=None, observation=None):
        self.movement, self.laser = movement, laser
        self.clock, self.sleep, self.cancel, self.emit, self.wall_clock = clock, sleep, cancel, emit, wall_clock
        self.axes = getattr(movement, 'measurement_axes', None) or axis_map(laser)
        self.transaction = transaction
        self.observation = observation

    def check(self):
        self.movement.tick()
        if self.transaction is not None:self.transaction.check()
        if getattr(self.movement, 'uncertain_output', False):
            raise DeviceError('Owner output is uncertain; no further change')

    def physical_outcome_on_error(self, error):
        return self.transaction.physical_outcome_on_error(error) if self.transaction is not None and getattr(self,'_parameter_application',False) else 'unknown'

    def finish(self, result):
        if self.transaction is not None:self.transaction.finish(result)

    def _procedure(self, changes, timeout_s=300.):
        p = validate(dict(moves=[dict(axis=c['parameter'], mode='auto', target={'value':c['value']}) for c in changes],
                          verify_samples=0, settle_s=0., timeout_s=timeout_s))
        return MeasurementMove(self.movement, p, self.axes, clock=self.clock, sleep=self.sleep,
                                  cancel=self.cancel, emit=self.emit, wall_clock=self.wall_clock)

    def preflight(self, changes, mode, measurements, planned=None):
        self.check()
        if self.transaction is not None and any(c['parameter'] not in self.axes['axes'] for c in changes):
            return self.transaction.preflight(changes, mode, measurements, planned=planned)
        if changes and mode != 'ordered':
            raise DeviceError('This owner axis adapter supports ordered application only; no silent downgrade')
        if measurements.get('dither') and not hasattr(self.movement, 'lock') and self.observation is None:
            raise DeviceError('Timestamped dither observation is unavailable on this owner path')
        if measurements.get('optical_quality') and hasattr(self.movement, 'port') and self.observation is None:
            raise DeviceError('No optical-quality binding is configured on this native current owner')
        state = dict(planned or {})
        resolved, checkpoint = [], {}
        for change in changes:
            name = change['parameter']
            if name not in self.axes['axes']:
                raise DeviceError('No validated owner binding for parameter ' + name)
            present = state.get(name)
            if present is None:
                present = self.movement.readback(name)
            checkpoint[name] = present
            value = change.get('value', present + change.get('offset', 0.))
            resolved.append(dict(parameter=name, value=value))
            state[name] = value
        if resolved:
            # Reuse the exact operating point path/refusal/range validation,
            # without opening a second lease or applying anything.
            procedure = self._procedure(resolved)
            procedure.plan_schedule()
        context = None
        if hasattr(self.movement,'validate_measurement_changes'):
            path = list(state.pop('_measurement_axis_path', [])) + resolved
            context = self.movement.validate_measurement_changes(path)
            state['_measurement_axis_path'] = path
        return dict(context=context, mode=mode, changes=resolved, checkpoint=checkpoint, planned=state,
                    measurements=copy.deepcopy(measurements), binding='operating_point_owner')

    def apply(self, plan, sample):
        self._parameter_application = plan.get('binding') != 'operating_point_owner'
        if plan.get('binding') != 'operating_point_owner':
            if self.transaction is None:
                raise DeviceError('No admitted transaction binding')
            return self.transaction.apply(plan, sample)
        for name, expected in plan['checkpoint'].items():
            policy = self.movement.policy(name)
            actual = self.movement.readback(name)
            if abs(actual-expected)>max(policy.resolution, 1e-9):
                raise DeviceError('Operating point changed since preflight: ' + name)
        started = self.wall_clock()
        # Sample before each RPC and after each verified step when supported.
        # Do not label these callback times as hardware acquisition times.
        def record(kind, data):
            self.emit(kind, data)
            if kind in ('operating_point_step', 'operating_point_move'):
                sample()
        procedure = self._procedure(plan['changes'], timeout_s=plan.get('runtime_timeout_s',300.))
        procedure.emit = record
        outcome = procedure.run()
        readback = {c['parameter']: self.movement.readback(c['parameter']) for c in plan['changes']}
        for c in plan['changes']:
            if abs(readback[c['parameter']]-c['value']) > max(self.movement.policy(c['parameter']).resolution,1e-9):
                raise DeviceError('Post-application readback differs')
        return dict(status='applied', mode='ordered', readback=readback, owner_result=outcome,
                    started_unix_s=started, completed_unix_s=self.wall_clock(),
                    timestamp_kind='host_request_and_verified_completion')

    @staticmethod
    def _block(frequency):
        stamp = frequency.get('acquired_unix_s')
        hz = frequency.get('frequency_hz')
        valid = (change_measure.finite(hz) and hz > 0 and change_measure.finite(stamp)
                 and frequency.get('acquisition_known', True) is True)
        return dict(acquired_unix_s=stamp, valid=valid,
                    values=dict(frequency_hz=hz, wavelength_nm=299792458e9/hz if valid else None))

    def read(self, measurements):
        if self.observation is not None:
            return self.observation(measurements)
        if not any(measurements.values()):
            return {}
        a = self.movement
        if (measurements.get('wavelength') and not measurements.get('optical_quality')
                and not measurements.get('dither') and hasattr(a, 'measure_frequency')):
            # The owner can expose a fast frequency source independently of its
            # optical-pattern cache. Combined requests retain one coherent frame.
            return dict(wavelength=self._block(a.measure_frequency()))
        if hasattr(a, 'measure_observation'):
            obs = a.measure_observation()
            frequency = obs['frequency']
        elif hasattr(a, 'lock'):
            row = a.info()
            obs = row['observation']
            frequency = obs['frequency']
        elif hasattr(a, 'ports') and 'observation' in a.ports:
            obs = asdict(a.ports['observation'].read_mode())
            frequency = obs['frequency']
        elif hasattr(a, 'port'):
            obs = {}
            frequency = a.state().get('optical') or {}
        else:
            raise MeasurementUnavailable('No timestamped measurement binding')
        result = {}
        if measurements.get('wavelength'):
            result['wavelength'] = self._block(frequency)
        if measurements.get('optical_quality'):
            metrics = obs.get('metrics') or {}
            result['optical_quality'] = dict(acquired_unix_s=frequency.get('acquired_unix_s'),
                valid=obs.get('quality_valid') is True and bool(metrics) and all(change_measure.finite(v) for v in metrics.values()), values=copy.deepcopy(metrics),
                single_mode=obs.get('single_mode'), mode_id=obs.get('mode_id'))
        if measurements.get('dither'):
            info = a.lock.response_dither_info()
            result['dither'] = dict(acquired_unix_s=info.get('dither_source_unix_s'),
                source_start_unix_s=info.get('dither_source_start_unix_s'),
                valid=info.get('dither_response_valid') is True and change_measure.finite(info.get('dither_response')),
                values=dict(response=info.get('dither_response')))
        return result


def execute_locked(request, movement, laser, *, clock, sleep, cancel, emit, controller_directory=None):
    original_cancel=cancel
    def cancel():
        if original_cancel():return True
        if controller_directory is None:return False
        root=Path(controller_directory)
        return any(any(root.glob(pattern)) for pattern in (
            'laser-reload-request-*.json','ff-requests/pending/*.json',
            'operating-point-requests/pending/*.json','runtime-parameter-request-*.json',
            'hot-control-request.json','target-ff-policy-request-*.json'))
    adapter = OperatingPointMeasurementAdapter(movement, laser, clock=clock, sleep=sleep, cancel=cancel, emit=emit)
    adapter.controller_directory = controller_directory
    adapter.directory = Path(controller_directory).parent / request['job_id'] if controller_directory and request.get('job_id') else None
    adapter.request_id = request.get('request_id')
    from ldcon.jobs.change_measure_parameters import ParameterMeasurementAdapter
    adapter.transaction = ParameterMeasurementAdapter(movement,laser,adapter.directory,controller_directory,
        clock=clock,sleep=sleep,cancel=cancel,emit=emit)
    runner = change_measure.run_batch if request['measurement_operation']=='measure.batch' else change_measure.run
    return runner(adapter, request['parameters'], clock=clock, sleep=sleep, cancel=cancel, emit=emit)


def open_measurement_ports(laser,card,p):
    """Never prepare/stop a controller as a side effect of measurement admission."""
    from ldcon.jobs.operating_point import lock_device,ECDLOpenAdapter
    _,_,device=lock_device(laser)
    if device['type']!='ecdl.rig.owner':
        return open_unlocked_ports(laser,card,p)
    from ldcon.jobs.ecdl_devices import OwnerSession,ModeProxy,AxisProxy,LoopProxy
    session=OwnerSession(device).connect(allow_prepare=False)
    ports=dict(observation=ModeProxy(session),loop=LoopProxy(session),
               **{axis:AxisProxy(session,axis) for axis in ('current','temperature','piezo')})
    try:
        movement=ECDLOpenAdapter(ports,laser)
    except BaseException:
        session.close()
        raise
    return ports,movement,session.close


def run_card(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None, open_ports=None):
    """Worker runner, sharing the lock's existing at-most-once operation queue."""
    batch = card['operation'] == 'measure.batch'
    p = (change_measure.batch_parameters if batch else change_measure.parameters)({'parameters':p}, {})
    if batch and not p['steps']:
        raise ValueError('Batch execution requires at least one step')
    steps = p['steps'] if batch else [p]
    if any(step['changes'] for step in steps) and not laser.get('allow_output', False):
        raise DeviceError('Laser output writes disabled')
    dlc = 'dlc_lock' in laser.get('interfaces', {})
    names = {c['parameter'] for step in steps for c in step['changes']}
    if 'target_frequency_hz' in names:
        if names != {'target_frequency_hz'} or dlc:
            raise DeviceError('Target-response transactions require a supported target-only sequence')
        from ldcon.jobs.change_measure_target import TargetMeasurementAdapter
        adapter = TargetMeasurementAdapter(directory,laser,card,clock=clock,sleep=sleep,cancel=cancel,emit=emit)
        runner = change_measure.run_batch if batch else change_measure.run
        try:
            return runner(adapter,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit)
        except BaseException as error:
            if (getattr(error,'measurement_result',None) or {}).get('physical_outcome')=='unknown':
                atomic_json(Path(directory)/'device-review.json',dict(source='change_measure_target',
                    physical_outcome='unknown',reason='Target application acknowledgement is unknown; never replay'))
            raise
    found = None if dlc else find_running_lock(directory, laser)
    if found is not None:
        lock_directory, meta, _ = found
        if not (lock_directory/'operating-point-requests'/'change-measure-v1').exists():
            raise DeviceError('Running lock has no change/measurement capability; update it before admission')
        queue = OperatingPointQueue(lock_directory)
        rid = uuid.uuid4().hex
        queue.submit(dict(request_id=rid, job_id=Path(directory).name, card_id=card['id'],
                          measurement_operation=card['operation'], parameters=p, submitted_unix_s=time.time()))
        started = clock()
        while True:
            if cancel() or clock()-started>=p['timeout_s']:
                queue.cancel(rid)
            status = queue.read_status(rid)
            if publish:
                publish(dict(status or {}, request_id=rid, lock_job=meta['id']))
            if status and status.get('status') in ('succeeded','measurement_incomplete'):
                return status['result']
            if status and status.get('status') in ('failed','cancelled','rejected'):
                result=status.get('result')
                if isinstance(result,dict):
                    emit('change_measure_batch_result' if batch else 'change_measure_result',result)
                    if result.get('physical_outcome')=='unknown':
                        atomic_json(Path(directory)/'device-review.json',dict(
                            source='change_measure',physical_outcome='unknown',request_id=rid,
                            lock_job=meta['id'],reason='Owner application or cleanup could not be confirmed; never replay'))
                error=(CardCancelled if status['status']=='cancelled' else DeviceError)(status.get('reason','Change/measurement failed'))
                if result is not None:error.measurement_result=result
                raise error
            if clock()-started>p['timeout_s']+5:
                result=dict(schema=change_measure.SCHEMA,status='failed',physical_outcome='unknown',
                    request_id=rid,lock_job=meta['id'],error='Cancellation acknowledgement missing; inspect owner, never replay',samples=[])
                emit('change_measure_batch_result' if batch else 'change_measure_result',result)
                atomic_json(Path(directory)/'device-review.json',result)
                error=DeviceError(result['error']);error.measurement_result=result
                raise error
            sleep(.1)
    if dlc and open_ports is None:
        from ldcon.jobs.change_measure_dlc import open_movement
        _, movement, close = open_movement(laser, card)
    else:
        opener = open_ports or open_measurement_ports
        _, movement, close = opener(laser, card, validate(dict(moves=None),require_moves=False))
    try:
        adapter = OperatingPointMeasurementAdapter(movement,laser,clock=clock,sleep=sleep,cancel=cancel,emit=emit)
        runner = change_measure.run_batch if batch else change_measure.run
        return runner(adapter,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit)
    except BaseException as error:
        if (getattr(error,'measurement_result',None) or {}).get('physical_outcome')=='unknown':
            atomic_json(Path(directory)/'device-review.json',dict(source='change_measure',
                physical_outcome='unknown',reason='Owner application or cleanup could not be confirmed; never replay'))
        raise
    finally:
        close()


def readiness(laser,card,p):
    """Static capability inspection; never opens an owner or promotes a value."""
    issues=[]
    batch=card['operation']=='measure.batch'
    steps=p.get('steps',[]) if batch else [p]
    if batch and not steps:issues.append('Add at least one explicit change/measurement step')
    interfaces=laser.get('interfaces',{})
    dlc='dlc_lock' in interfaces
    binding=interfaces.get('dlc_lock') or interfaces.get('lock') or interfaces.get('native_current')
    if not binding:
        return issues+['A supported laser owner binding is required']
    device=laser.get('devices',{}).get(binding.get('device'),{})
    kind=device.get('type')
    if kind not in ('dlcpro.laser.owner','ecdl.rig.owner','esp32servocon.native_current.owner'):
        issues.append('No change/measurement adapter for '+str(kind))
    if dlc:
        if not laser.get('allow_output',False):issues.append('DLC measurement session admission requires laser outputs enabled; no implicit enabling')
        axes=set(device.get('control_settings',{}).get('policy',{}).get('axes',{}))
        if not device.get('control_settings',{}).get('commissioned'):
            issues.append('DLC commissioning is incomplete')
    else:
        try:
            mapped_axes=axis_map(laser)['axes']
            axes=set(mapped_axes)
        except (DeviceError,ValueError,KeyError):mapped_axes={};axes=set()
    parameter_fields={'target_frequency_hz','kp_count_per_nm','ki_count_per_nm_s','integral_limit_count','tec_gain','tec_dt_cap_s',
                      'supervision_interval_s','piezo_center_dac','current_per_temperature'}
    all_parameters={c['parameter'] for step in steps for c in step.get('changes',[])}
    if 'target_frequency_hz' in all_parameters and all_parameters!={'target_frequency_hz'}:
        issues.append('Target history steps cannot share a batch with axis/PI/FF changes; use distinct cards')
    for step in steps:
        changes=step.get('changes',[]);measurements=step.get('measurements',{})
        if changes and not laser.get('allow_output',False):issues.append('Laser output writes disabled')
        requested={c['parameter'] for c in changes}
        if not dlc:
            for name in requested & axes:
                capability=mapped_axes[name]
                if capability['locked']==capability['open_loop']=='refused':
                    issues.append('Owner cannot change '+name+': '+capability.get('open_loop_reason','axis is disabled'))
        if 'target_frequency_hz' in requested and (dlc or requested!={'target_frequency_hz'}):
            issues.append('Target-response binding requires a supported target-only step')
        if 'target_frequency_hz' in requested and (step['apply_mode']!='ordered' or measurements.get('optical_quality') or measurements.get('dither')):
            issues.append('Target history binding requires ordered application and wavelength-only measurements')
        runtime_fields=requested-axes-{'target_frequency_hz'}
        if runtime_fields:
            from ldcon.core.adjustment_registry import PARAMETER_FAMILIES
            families={'pi':{'kp_count_per_nm','ki_count_per_nm_s','integral_limit_count'}}
            if kind=='ecdl.rig.owner':families.update({name:set(value) for name,value in PARAMETER_FAMILIES.items()})
            selected=[name for name,fields in families.items() if runtime_fields<=fields]
            if requested & axes or len(selected)!=1:
                issues.append('No same-owner transaction for this parameter combination; use supported explicit batch steps')
            elif not dlc:
                from ldcon.agents.measurement_grant import freeze_measurement_plan
                try:plan=freeze_measurement_plan(laser)
                except (ValueError,TypeError):
                    plan=None;issues.append('Saved laser measurement_plan is invalid')
                family=selected[0]
                if plan is None or family not in plan.get('families',[]):
                    issues.append('An enabled approved laser measurement_plan for '+family+' is required')
                elif family=='pi' and plan['pid_parameters'].get('strategy')!='validated_candidates':
                    issues.append('PI measurement_plan must admit independent validated_candidates')
        if requested-axes and (dlc or requested-axes-parameter_fields):
            issues.append('No validated binding for '+', '.join(sorted(requested-axes)))
        if requested & axes and step['apply_mode']!='ordered':
            issues.append('Axis owner supports ordered application; atomic/coordinated unavailable')
        if measurements.get('dither') and (dlc or kind=='esp32servocon.native_current.owner'):
            issues.append('Timestamped dither measurement is unavailable on this owner')
        if measurements.get('optical_quality') and kind=='esp32servocon.native_current.owner':
            issues.append('No optical quality binding is configured on this native current owner')
    return list(dict.fromkeys(issues))
