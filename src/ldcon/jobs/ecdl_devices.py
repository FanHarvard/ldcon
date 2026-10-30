"""Card-side typed proxies. Physical device I/O lives entirely in ECDLOwner."""
import json
import os
import time
from urllib.request import Request,build_opener,ProxyHandler

from ldcon.core.contracts import FrequencySample,DeviceError,MeasurementUnavailable,OwnerUnavailable
from ldcon.owners.ecdl_prelock import AxisPolicy,ModeObservation
from ldcon.core.interface_schema import digest
from ldcon.core.instrument_client import validate_endpoint,BrokerError


def connection_refused(error):
    """The request never reached the owner (connection refused, also wrapped in urllib's URLError)."""
    return isinstance(error,ConnectionRefusedError) or isinstance(getattr(error,'reason',None),ConnectionRefusedError)


def owner_unavailable(error,what):
    return OwnerUnavailable('ECDL rig owner unavailable (%s): %s: %s'%(what,type(error).__name__,error))


class OwnerSession:
    def __init__(self,device):
        self.device=device;self.endpoint=validate_endpoint(device['connection']['endpoint'])
        self.lease=None;self.uncertain_output=False;self.recovering=False
        self.cached=None;self.cached_at=0.
        self.http=build_opener(ProxyHandler({}))

    def rpc(self,action,_timeout=30,**values):
        payload=dict(action=action,**values)
        if self.device['connection'].get('instrument'):
            payload=dict(action='ecdl',instrument=self.device['connection']['instrument'],request=payload)
        request=Request(self.endpoint+'/rpc',data=json.dumps(payload).encode(),
                        headers={'Content-Type':'application/json'})
        token=os.environ.get('LDCON_INSTRUMENT_TOKEN')
        if token:request.add_header('Authorization','Bearer '+token)
        with self.http.open(request,timeout=_timeout) as response:reply=json.load(response)
        if reply.get('ok') is not True:raise BrokerError(reply.get('code','owner'),reply.get('error','Owner request rejected'))
        return reply['result']

    def connect(self, *, allow_prepare=True):
        if not allow_prepare:
            state=self.rpc('inspect')
            if state.get('fault') or state.get('lease_retained') or state['snapshot'].get('open_loop') is not True:
                raise DeviceError('Intentional branch hop requires an idle open-loop owner; stop the lock first')
        description=self.rpc('describe')
        if digest(self.device) not in (description['revision'],description.get('policy_adoption',{}).get('saved_revision')):
            raise DeviceError('ECDL definition differs from saved and effective owner revisions')
        # This claim cannot open the servo or move TEC/PZT. It permits initial
        # current mapping even when a multimode spectrum has no wavelength.
        try:
            result=self.rpc('claim',revision=digest(self.device),owner='ldcon ECDL initial point')
        except BrokerError as error:
            if error.code!='measurement':raise
            result=self.rpc('claim',revision=digest(self.device),owner='ldcon ECDL initial point',current_recovery=True)
            self.recovering=True
        self.lease=result['lease'];self.policies={k:AxisPolicy(**v) for k,v in result['policies'].items()}
        try:
            if not allow_prepare and self.snapshot()['open_loop'] is not True:
                raise DeviceError('PI became active; branch hop does not stop it')
            if self.device['control_settings']['prepare_open_loop'] and not self.snapshot()['open_loop']:
                self.uncertain_output=True;self.call('prepare');self.uncertain_output=False
            return self
        except BaseException as error:
            if isinstance(error,BrokerError) and error.code in ('range','configuration','ownership'):
                self.uncertain_output=False
            if self.uncertain_output:error.ecdl_review=self.review_details()
            self.close();raise

    def call(self,action,**values):
        if self.lease is None:raise DeviceError('ECDL rig lease is closed')
        return self.rpc(action,lease=self.lease,**values)

    def snapshot(self):
        if self.cached is None or time.monotonic()-self.cached_at>.1:
            self.cached=self.call('read');self.cached_at=time.monotonic()
        return self.cached

    def close(self):
        if self.lease is not None and not self.uncertain_output:
            self.call('release');self.lease=None

    def review_details(self):
        return dict(endpoint=self.endpoint,lease=self.lease,instrument=self.device['connection'].get('instrument'),
                    message='Uncertain ECDL owner write; rig lease retained. Review owner audit before restart.')


class Proxy:
    def __init__(self,session):self.session=session
    @property
    def uncertain_output(self):return self.session.uncertain_output
    def review_details(self):return self.session.review_details()
    def close(self):self.session.close()


class AxisProxy(Proxy):
    def __init__(self,session,axis):
        super().__init__(session);self.axis=axis;self.policy=session.policies[axis]
    def readback(self):return self.session.snapshot()['outputs'][self.axis]
    def begin_recovery(self):
        self._recovery_state('begin_current_recovery');self.session.recovering=True
    def end_recovery(self):
        self._recovery_state('end_current_recovery');self.session.recovering=False
    def _recovery_state(self,action):
        try:return self.session.call(action)
        except BrokerError as error:
            if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
            raise
    def move(self,value,duration_s):
        self.session.uncertain_output=True
        try:self.session.cached=self.session.call('recover_current' if self.axis=='current' and self.session.recovering else 'move',axis=self.axis,value=value)
        except BrokerError as error:
            if error.code in ('range','configuration','ownership','measurement'):self.session.uncertain_output=False
            if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
            raise
        self.session.cached_at=time.monotonic()
        self.session.uncertain_output=False


class ModeProxy(Proxy):
    def read_mode(self):
        return self._read(False)
    def read_mode_with_patterns(self):
        return self._read(True)
    def _read(self,include_patterns):
        try:r=self.session.call('observe',**({'include_patterns':True} if include_patterns else {}))
        except BrokerError as error:
            if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
            raise
        return ModeObservation(FrequencySample(**r['frequency']),r['single_mode'],r['mode_id'],
                               r['quality_valid'],r['impurity'],r['maintenance_required'],dict(r.get('metrics') or {}),dict(r.get('fringe_analysis') or {}))


class LoopProxy(Proxy):
    def is_open_loop(self):return self.session.snapshot()['open_loop']


def open_ecdl_interfaces(laser,card):
    bindings=[laser['interfaces'][name] for name in card['uses']]
    if len({b['device'] for b in bindings})!=1:raise ValueError('ECDL interfaces must share one owner')
    device=laser['devices'][bindings[0]['device']]
    session=OwnerSession(device).connect(allow_prepare=card['operation']!='tune.ecdl_branch_hop')
    ports=dict(observation=ModeProxy(session),piezo=AxisProxy(session,'piezo'),
                temperature=AxisProxy(session,'temperature'),current=AxisProxy(session,'current'),loop=LoopProxy(session))
    if card['operation']=='study.ecdl_current_response':ports['lock']=SteadyLockProxy(session)
    return ports


class SteadyLockProxy(Proxy):
    """Native lifecycle requests only; never connect to a physical instrument."""
    supports_validated_candidates = True
    def _call(self,action,mutating=False,**values):
        if mutating:self.session.uncertain_output=True
        try:result=self.session.call(action,**values)
        except BrokerError as error:
            if error.code in ('range','configuration','ownership','measurement'):
                self.session.uncertain_output=False
            if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
            raise
        if mutating:
            self.session.uncertain_output=bool(isinstance(result,dict) and result.get('requires_review'))
            # Native hold/arm and owner-planned adjustments can change actual
            # DACs outside AxisProxy.move. A following replay must inspect the
            # newly held state, never the previous open-loop snapshot.
            self.session.cached=None
        return result
    def prepare_runtime(self, parameters):
        """Read the owner contract before arm; legacy owners get no new RPCs."""
        description = self.session.rpc('describe')
        if description['revision'] != digest(self.session.device):
            raise DeviceError('ECDL definition changed before runtime negotiation')
        version = description.get('capabilities', {}).get('steady_runtime_parameters')
        self.runtime_owner_parameters = type(version) is int and version == 1
        if not self.runtime_owner_parameters:
            profile = self.session.device['control_settings']['steady_lock']
            values = dict(tec_gain=profile['tec_gain'],
                          tec_ff_paused=profile.get('hot_control', {}).get('initial_tec_ff_paused', False),
                          current_ff_enabled=bool(profile.get('tec_current_feedforward')))
            for name, value in values.items():
                if parameters.get(name) is not None and parameters[name] != value:
                    raise DeviceError('Legacy owner cannot restore differing initial ' + name + '; prepare matching owner configuration before arm')
            self.legacy_runtime_values = values
        return dict(owner_parameters_supported=self.runtime_owner_parameters,
                    reason=None if self.runtime_owner_parameters else
                    '既存ownerは版付きパラメーター変更に未対応です。周期・待機期限・実行時間だけ変更できます。',
                    unsupported_fields=[] if self.runtime_owner_parameters else
                    ['target_frequency_hz', 'tolerance_hz', 'strategy', 'tec_gain', 'tec_ff_paused', 'current_ff_enabled'])
    def check_ready(self,strategy):
        deadline=time.monotonic()+5.
        while True:
            try:return self._call('steady_ready',strategy=strategy)
            except MeasurementUnavailable:
                if time.monotonic()>=deadline:raise
                time.sleep(.1)
    def arm(self,target,tolerance,strategy):
        deadline=time.monotonic()+5.
        while True:
            try:
                return self._call('steady_arm',True,target_frequency_hz=target,tolerance_hz=tolerance,strategy=strategy)
            except MeasurementUnavailable:
                # Owner measurement rejections are confirmed before any arm
                # write. A new source frame can fail after check_ready passed.
                # Unknown/post-write outcomes never enter this retry path.
                if self.uncertain_output or time.monotonic()>=deadline:raise
                time.sleep(.1)
                self.check_ready(strategy)
    # Owner restart (design §5, 2026-10-27): a transport failure of the liveness RPC or of the cycle read is the owner
    # being down/restarting, not a lost lock: OwnerUnavailable, retried by the card with the same lease for a bounded
    # time (owner_outage_timeout_s). Nothing is written by this worker meanwhile; the firmware PZT PI runs on.
    def heartbeat(self):
        try:return self._call('steady_heartbeat')
        except OSError as error:raise owner_unavailable(error,'steady_heartbeat') from error
    def read_cycle(self):
        try:return self._call('steady_read',True)
        except OSError as error:
            # steady_read may ramp the target (a write): a refused connection never reached the owner, so the output
            # is certain; a timeout/reset keeps uncertain_output until the next successful lease-bound call.
            if connection_refused(error):self.session.uncertain_output=False
            raise owner_unavailable(error,'steady_read') from error
    def adjust(self,proposal):return self._call('steady_adjust',True,proposal=proposal)
    def hold(self,reason):return self._call('steady_hold',True,reason=reason)
    def pid_begin(self,parameters):return self._call('steady_pid_begin',parameters=parameters)
    def pid_target(self,pid):return self._call('steady_pid_target',pid=pid)
    def pid_save(self,pid):return self._call('steady_pid_save',True,pid=pid)
    def pid_finish(self,status):return self._call('steady_pid_finish',status=status)
    def study_start(self,parameters):return self._call('steady_study_start',parameters=parameters)
    def study_status(self):return self._call('steady_study_status')
    def study_stop(self):return self._call('steady_study_stop')
    def response_info(self, *, include_patterns=False):
        result=self._call('response_info', **({'include_patterns': True} if include_patterns else {}))
        self.session.cached=None
        return result
    def hot_control(self,command):return self._call('steady_hot_control',command=command)
    def parameters_status(self):return self._call('steady_parameters_status')
    def reload_settings(self,device,paths,decision_id=None):
        result=self._call('settings_reload',device=device,paths=list(paths),decision_id=decision_id)
        if result.get('policies'):
            self.session.policies={key:AxisPolicy(**value) for key,value in result['policies'].items()}
        return result
    def update_parameters(self,expected_revision,patch):
        return self._call('steady_parameters',True,expected_revision=expected_revision,patch=patch)
    def response_begin(self):return self._call('response_begin')
    def response_end(self):return self._call('response_end')
    def background_status(self):return self._call('background_status')
    def background_reserve(self,request_ids):return self._call('background_reserve',request_ids=list(request_ids))
    def background_release(self,request_ids):return self._call('background_release',request_ids=list(request_ids))
    def background_begin(self,**values):return self._call('background_begin',**values)
    def background_current(self,token,value):return self._call('background_current',True,token=token,value=value)
    def background_restore(self,token,reason):return self._call('background_restore',True,token=token,reason=reason)
    def background_adopt(self,token):return self._call('background_adopt',True,token=token)
    def background_abandon(self,reason):return self._call('background_abandon',reason=reason)
    def background_parameters_target(self,token):return self._call('background_parameters_target',True,token=token)
    def background_pid_target(self,token,pid):return self._call('background_pid_target',token=token,pid=pid)
    def background_pid_finish(self,token,status):return self._call('background_pid_finish',token=token,status=status)
    # Rule F generic cards (2026-10-22): the references the running lock holds, and the ONE target.
    def set_reference(self,axis,value):return self._call('steady_set_reference',True,axis=axis,value=value)
    def set_target(self,target_frequency_hz,*,target_ff_study_token=None):
        return self._call('steady_set_target',True,target_frequency_hz=target_frequency_hz,
                          **({'target_ff_study_token':target_ff_study_token} if target_ff_study_token else {}))
    def target_ff_study_status(self):return self._call('steady_target_ff_study_status')
    def target_ff_study_set(self,expected_revision,*,scale,step_hz,duration_s,study_token=None,thermal=None):
        return self._call('steady_target_ff_study_set',expected_revision=expected_revision,scale=scale,
                          step_hz=step_hz,duration_s=duration_s,study_token=study_token,
                          **({'thermal':thermal} if thermal is not None else {}))
    def target_ff_study_restore(self,expected_revision,study_token):
        return self._call('steady_target_ff_study_restore',expected_revision=expected_revision,study_token=study_token)
    def response_current(self,value):return self._call('response_current',True,value=value)
    def response_dither_info(self):return self._call('response_dither_info')
    def response_dither(self):return self._call('response_dither')
    def recovery_ports(self):
        return dict(lock=self,current=AxisProxy(self.session,'current'),
                    piezo=AxisProxy(self.session,'piezo'),temperature=AxisProxy(self.session,'temperature'),
                    observation=ModeProxy(self.session),loop=LoopProxy(self.session))
    def check_commission_ready(self,target,tolerance):return self._call('commission_ready',target_frequency_hz=target,tolerance_hz=tolerance)
    def load_reviewed_dither(self,evidence):return self._call('load_reviewed_dither',evidence=str(evidence))
    def configure_dither(self,target,tolerance):return self._call('configure_dither',True,target_frequency_hz=target,tolerance_hz=tolerance)
    def commission_arm(self,target,tolerance):return self._call('commission_arm',True,target_frequency_hz=target,tolerance_hz=tolerance)
    def commission_next(self):return self._call('commission_next')
    def commission_raise_bound(self):return self._call('commission_raise_bound',True)
    def commission_fixed_stop(self):return self._call('commission_fixed_stop',True)
    def commission_actual_hold(self):return self._call('commission_actual_hold',True)
    def commission_hold_sample(self):return self._call('commission_hold_sample')
    def commission_complete(self):return self._call('commission_complete')


def open_steady_interfaces(laser,card):
    binding=laser['interfaces']['lock'];device=laser['devices'][binding['device']]
    # Deliberately bypass initial-point connect(): no prepare/open-loop action
    # and no current-only recovery claim is permitted during a steady handover.
    session=OwnerSession(device)
    description=session.rpc('describe')
    if digest(device) not in (description['revision'],description.get('policy_adoption',{}).get('saved_revision')):
        raise DeviceError('ECDL definition differs from saved and effective owner revisions')
    deadline=time.monotonic()+5.
    while True:
        try:
            result=session.rpc('claim',revision=digest(device),owner='ldcon ECDL stationary lock')
            break
        except BrokerError as error:
            # Measurement rejection occurs before lease creation, with no I/O
            # writes. Never retry unknown I/O or an ownership outcome.
            if error.code!='measurement' or time.monotonic()>=deadline:raise
            time.sleep(.1)
    session.lease=result['lease']
    session.policies={k:AxisPolicy(**v) for k,v in result['policies'].items()}
    return dict(lock=SteadyLockProxy(session))
