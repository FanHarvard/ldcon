"""Card/owner protocol for Ethernet host PI and native DLC scans.

Card processes contact only the laser owner, never the chassis or wavemeter.
All algorithm parameters are explicit deck/assignment data, not code defaults.
"""
from copy import deepcopy
from dataclasses import asdict
import math
import time
import uuid

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_owner import number
from ldcon.jobs.ecdl_devices import OwnerSession
from ldcon.owners.ecdl_prelock import AxisPolicy

OPERATIONS = {'servo.dlcpro_frequency', 'sweep.dlcpro_frequency'}
CONTROL_KEYS = {'kp','ki_per_s','integral_limit_v','capture_hz','tolerance_hz','tec_gain','tec_period_s','pzt_center_v'}
BASE_KEYS = CONTROL_KEYS | {'duration_s','sample_interval_s','target_frequency_hz'}
SCAN_KEYS = {'period_s','span_hz','shape','center_lock','max_gap_s','boundary_tolerance_s'}


def parameters(card, overrides):
    p = {**card['parameters'], **overrides}
    keys = BASE_KEYS | (SCAN_KEYS if card['operation']=='sweep.dlcpro_frequency' else set())
    if set(p) != keys:
        raise ValueError('Missing/unknown DLC card parameters: '+str(sorted(set(p)^keys)))
    for key in keys-{'shape','center_lock','target_frequency_hz','pzt_center_v'}:
        number(p[key])
    if p['pzt_center_v'] is not None:number(p['pzt_center_v'])
    if p['target_frequency_hz'] is not None and number(p['target_frequency_hz']) <= 0:
        raise ValueError('Nonpositive optical target')
    if min(p['duration_s'],p['sample_interval_s'],p['capture_hz'],p['tolerance_hz'],p['integral_limit_v'],p['tec_period_s']) <= 0:
        raise ValueError('Nonpositive DLC duration/control parameter')
    if min(p['kp'],p['ki_per_s'],p['tec_gain']) < 0:
        raise ValueError('Negative normalized gain')
    if 'shape' in p:
        if p['shape'] not in ('triangle','sine') or type(p['center_lock']) is not bool:
            raise ValueError('Invalid scan shape/center lock')
        if min(p['period_s'],p['span_hz'],p['max_gap_s'],p['boundary_tolerance_s']) <= 0 or p['max_gap_s']>=p['period_s']/2:
            raise ValueError('Invalid scan cadence/coverage')
    return p


def readiness(laser, card, p):
    issues=[]
    if not laser['allow_output']:issues.append('Laser output writes disabled')
    try:
        role='axis' if card['operation']=='identify.axis_frequency' else 'dlc_lock'
        device=laser['devices'][laser['interfaces'][role]['device']]
        if not device['connection']['endpoint']:issues.append('Owner endpoint required')
        if device['type']=='dlcpro.laser.owner':
            if device['control_settings'].get('commissioned') is not True:
                issues.append('DLC channel, output filters, optical source and FF commissioning required')
            if card['operation']!='identify.axis_frequency':
                for axis in ('piezo','temperature'):
                    if device['control_settings']['policy']['axes'][axis]['frequency_hz_per_unit'] is None:
                        issues.append(axis+' optical frequency calibration required')
        elif card['operation']!='identify.axis_frequency':
            issues.append('DLC laser owner required')
        if card['operation']!='identify.axis_frequency':
            from ldcon.jobs.lock_target import resolve_target
            if resolve_target(laser,p) is None:issues.append('Optical target is unset')
    except (KeyError, TypeError, ValueError) as error:
        issues.append('Incomplete owner configuration: '+str(error))
    return issues


class Session(OwnerSession):
    def rpc(self, action, _timeout=30, **values):
        return super().rpc(action,_timeout=_timeout,laser_id=self.device['connection']['laser_id'],**values)

    def connect(self):
        description=self.rpc('describe')
        if description['revision']!=digest(self.device):
            raise DeviceError('DLC laser definition differs from owner')
        result=self.rpc('claim',revision=digest(self.device))
        self.lease=result['lease']
        self.policies={key:AxisPolicy(**value) for key,value in result['policies'].items()}
        return self

    def action(self, action, **values):
        self.uncertain_output=True
        from ldcon.core.instrument_client import BrokerError
        try:
            result=self.call(action,**values)
        except BrokerError as error:
            if error.code in ('range','configuration','ownership','measurement'):
                self.uncertain_output=False
            raise
        self.uncertain_output=False
        return result


def open_ports(laser, card):
    if not laser['allow_output']:
        raise DeviceError('Laser output writes disabled')
    device=laser['devices'][laser['interfaces']['dlc_lock']['device']]
    if device['control_settings'].get('commissioned') is not True:
        raise DeviceError('DLC commissioning incomplete')
    return {'dlc_lock':Session(device).connect()}


def trace_record(snapshot, *, tolerance_hz, max_sample_age_s):
    """Project owner evidence for Web/telemetry without changing control."""
    row=deepcopy(snapshot)
    observation=snapshot.get('observation') or {}
    frequency,target=observation.get('frequency_hz'),snapshot.get('target_frequency_hz')
    acquired,observed=observation.get('acquired_s'),snapshot.get('observed_s')
    finite=lambda x:type(x) in (int,float) and math.isfinite(x)
    age=observed-acquired if finite(observed) and finite(acquired) else None
    fresh=finite(age) and 0<=age<=max_sample_age_s and finite(frequency) and frequency>0
    error=frequency-target if finite(frequency) and finite(target) else None
    locked=bool(snapshot.get('pi_enabled') is True and snapshot.get('mode')=='lock'
                and not snapshot.get('fault') and fresh and finite(error) and abs(error)<=tolerance_hz)
    if snapshot.get('fault'):state='failed'
    elif snapshot.get('mode')=='idle':state='hold'
    elif snapshot.get('mode') in ('scan','awg_sweep'):state='sweeping'
    elif not fresh:state='waiting_for_measurement'
    elif locked:state='mode_degraded' if observation.get('degraded') is True else 'locked'
    else:state='tracking'
    row.update(state=state,locked=locked,frequency_hz=frequency,error_hz=error,
        wavelength_nm=299792458e9/frequency if finite(frequency) and frequency>0 else None,
        sample_age_s=age,source_acquired_unix_s=observation.get('acquired_unix_s'),
        metrics=observation.get('metrics') or {})
    return row


def run(ports, laser, card, p, *, clock, sleep, cancel, emit):
    from ldcon.jobs.lock_target import resolve_target
    target=resolve_target(laser,p)
    if target is None:raise ValueError('Target required')
    session=ports['dlc_lock']
    p=dict(p)
    if p['pzt_center_v'] is None:p['pzt_center_v']=session.device['control_settings']['policy']['pzt_center_v']
    if cancel():raise CardCancelled('Cancelled before DLC start')
    settings={key:value for key,value in p.items() if key not in ('duration_s','sample_interval_s','target_frequency_hz')}
    session.action('start_scan' if card['operation'].startswith('sweep.') else 'start_lock',target=target,settings=settings)
    started=clock()
    last_state=None
    try:
        while clock()-started<p['duration_s']:
            if cancel():raise CardCancelled('DLC card cancelled')
            state=session.call('heartbeat')
            record=trace_record(state,tolerance_hz=p['tolerance_hz'],
                max_sample_age_s=session.device['control_settings']['policy']['max_sample_age_s'])
            if record['state']!=last_state:
                emit('state',{'state':record['state']});last_state=record['state']
            emit('trace',record)
            if state['fault']:raise DeviceError('DLC owner fault: '+state['fault'])
            if state['mode']=='idle':raise DeviceError('DLC owner held before card completion')
            sleep(p['sample_interval_s'])
    finally:
        # Protocol stop is idempotent only on this live lease. An uncertain
        # submission is retained, never hidden by speculative stop/replay.
        if not session.uncertain_output:
            session.action('stop')
    return dict(status='succeeded',message='DLC procedure complete; output held',target_frequency_hz=target)


class Protocol:
    """One externally leased laser endpoint; instrument ownership stays separate."""
    def __init__(self, owner, device):
        self.owner=owner
        self.device=deepcopy(device)
        self.client=None
        from ldcon.owners.dlcpro_client_liveness import ClientLiveness
        self.client_liveness = ClientLiveness(self)
        owner.credit_waiting_client = self.client_liveness.credit_verified_boundary

    def policies(self):
        result={}
        for axis,p in self.owner.policy['axes'].items():
            span=p['bounds'][1]-p['bounds'][0]
            result[axis]=asdict(AxisPolicy(p['unit'],*p['bounds'],span,span/p['update_interval_s'],
                p['update_interval_s'],p['settle_s'],p['frequency_hz_per_unit'],
                self.owner.laser_id+'.'+axis,digest(p),p['readback_tolerance']))
        return result

    def dispatch(self, request):
        with self.client_liveness.incoming(request):
            return self._dispatch(request)

    def _dispatch(self, request):
        if request.get('action') == 'operating_mode_status':
            if request.get('laser_id') != self.owner.laser_id:
                raise PermissionError('Wrong laser scope')
            from ldcon.owners.dlcpro_operating_mode import dispatch
            return dispatch(self, 'operating_mode_status', {})
        if request.get('action') == 'stop':
            manager = getattr(self.owner, 'awg_sweep', None)
            with self.owner.mutex:
                if (request.get('laser_id') != self.owner.laser_id
                        or self.client is None or request.get('lease') != self.client):
                    raise PermissionError('Client lease mismatch')
                waiting = manager is not None and manager.preparing()
                if waiting:
                    manager.stop()
            if waiting and not manager.prepared.wait(20.):
                raise DeviceError('Sweep preparation cleanup pending; controller lease retained')
        with self.owner.mutex:
            r=dict(request)
            action=r.pop('action')
            if r.pop('laser_id')!=self.owner.laser_id:
                raise PermissionError('Wrong laser scope')
            if action in ('operating_mode_status', 'operating_mode_change'):
                from ldcon.owners.dlcpro_operating_mode import dispatch
                return dispatch(self, action, r)
            if action=='describe':
                return dict(revision=digest(self.device),policies=self.policies())
            if action=='inspect':
                from ldcon.owners.dlcpro_operating_mode import status as operating_mode_status
                return dict(snapshot=self.owner.snapshot(cached=True),client_claimed=self.client is not None,
                            operating_mode=operating_mode_status(self.owner))
            if action=='claim':
                if self.client or r.get('revision')!=digest(self.device):
                    raise PermissionError('Lease active or revision mismatch')
                self.owner._healthy()
                if self.owner.mode!='idle':raise ValueError('Owner must be idle')
                self.client=uuid.uuid4().hex
                self.owner.heartbeat()
                return dict(lease=self.client,policies=self.policies())
            if action.startswith('awg_sweep_'):
                from ldcon.owners.dlcpro_awg_protocol import dispatch
                return dispatch(self, action, r)
            if action.startswith('target_change_'):
                from ldcon.owners.dlcpro_target_changes import dispatch
                return dispatch(self, action, r)
            if action.startswith('pi_trial_'):
                from ldcon.owners.dlcpro_pi_protocol import dispatch
                return dispatch(self, action, r)
            if self.client is None or r.pop('lease',None)!=self.client:
                raise PermissionError('Client lease mismatch')
            self.owner.heartbeat()
            if action=='read' or action=='heartbeat':return self.owner.snapshot(cached=True)
            if action=='observe':return asdict(self.owner.read_optical())
            if action=='observe_frequency':return asdict(self.owner.read_control())
            if action=='validate_moves':
                from ldcon.jobs.change_measure_dlc import validate_owner_moves
                return validate_owner_moves(self.owner,r['changes'])
            if action=='move':
                self.owner.move(r['axis'],r['value'])
                return self.owner.snapshot()
            if action=='release':
                if (self.owner.mode!='idle' or self.owner.fault
                        or (getattr(self.owner,'awg_sweep',None) and self.owner.awg_sweep.active())):
                    raise ValueError('Active/faulted owner lease retained')
                self.client=None
                return dict(released=True)
            if action=='start_lock':self.owner.start_lock(r['target'],r['settings'])
            elif action=='start_scan':self.owner.start_scan(r['target'],r['settings'])
            elif action=='stop':return self.owner.stop()
            elif action=='set_target':return dict(revision=self.owner.set_target(r['target'],r['expected_revision']))
            else:raise ValueError('Unsupported DLC operation')
            return self.owner.snapshot()
