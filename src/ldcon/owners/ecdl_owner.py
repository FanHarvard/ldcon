"""Exclusive configured ECDL rig owner: typed RPC, one process for every device I/O."""
import argparse
from dataclasses import asdict, replace
import fcntl
import hashlib
import copy
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
import uuid

from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.device_conditions import finite
from ldcon.owners.ecdl_prelock import AxisPolicy
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import load_mapping, digest
from experiment_core.errors import OperationError, UncertainWrite, AuthorizationRequired, StaleContext


# Rule M step 1 (2026-10-27): the authority's ``calibration`` dependency is the physical calibration of the axes
# only. The operating timing (update_interval_s, settle_s) is an operating condition adopted live, so it must not
# change the dependencies (a changed dependency makes every projection install raise StaleContext until restart).
CALIBRATION_KEYS=('unit','identity','frequency_hz_per_unit','resolution','readback_tolerance','coefficient_source')


def calibration_dependency(axes):
    return digest({axis:{key:settings.get(key) for key in CALIBRATION_KEYS} for axis,settings in axes.items()})


def _jsonable(value):
    """json.dumps default for the durable owner state: numpy scalars/arrays become Python numbers/lists."""
    if hasattr(value,'tolist'):return value.tolist()
    if hasattr(value,'item'):return value.item()
    raise TypeError('not JSON serializable: '+type(value).__name__)


def policy_dependencies(laser):
    rig=laser['devices']['rig']
    return dict(connection=digest(rig['connection']),calibration=calibration_dependency(rig['control_settings']['axes']))


def migrate_policy_dependencies(journal, laser, dependencies):
    """Upgrade an authority journal written before step 1 (calibration = digest of the whole axes block).

    Only when the saved dependency equals the old formula applied to the SAVED authority document (so no
    calibration change is hidden by the migration) and no adoption is pending. Returns the migrated record or None.
    """
    from experiment_core.configuration import Configuration, Parameter
    configuration=Configuration(journal,laser['id']+'.policy',{'document':Parameter('safety','','','cycle')},
                                {'document':copy.deepcopy(laser)},dependencies=copy.deepcopy(dependencies))
    state=configuration.state()
    saved=state['dependencies']
    if saved==dependencies or state['pending'] is not None or set(saved)!=set(dependencies):
        return None
    document=state['value']['document']
    try:legacy=digest(document['devices']['rig']['control_settings']['axes'])
    except (KeyError,TypeError):return None
    if saved.get('connection')!=dependencies['connection'] or saved.get('calibration')!=legacy:
        return None
    if calibration_dependency(document['devices']['rig']['control_settings']['axes'])!=dependencies['calibration']:
        return None
    configuration.set_dependencies(dependencies)
    return dict(before=saved,after=dependencies)


def axis_policies(device):
    result={}
    for axis in ('piezo','temperature','current'):
        v=device['absolute_conditions']['axes'][axis];s=device['control_settings']['axes'][axis]
        if not isinstance(v.get('bounds'),list) or len(v['bounds'])!=2:
            raise ValueError(axis+' approved bounds are unset')
        span=finite(v['bounds'][1])-finite(v['bounds'][0])
        if axis=='temperature' and v.get('max_step') is None:
            from ldcon.owners.tec_rate import delivery_step
            step=delivery_step(v['max_slew_per_s'],s['update_interval_s'])
        else:
            step=span if axis=='piezo' and v['max_step'] is None else v['max_step']
        slew=span/s['update_interval_s'] if axis=='piezo' and v['max_slew_per_s'] is None else v['max_slew_per_s']
        p=AxisPolicy(unit=s['unit'],low=v['bounds'][0],high=v['bounds'][1],
            max_step=step,max_slew_per_s=slew,
            update_interval_s=s['update_interval_s'],settle_s=s['settle_s'],
            frequency_hz_per_unit=s['frequency_hz_per_unit'],identity=s['identity'],revision=s['revision'],
            resolution=s.get('resolution',0.))
        p.validate()
        if axis=='temperature' and p.frequency_hz_per_unit is None:
            raise ValueError('Temperature tuning coefficient is unset')
        bounds=v['activation_range']
        if not isinstance(bounds,list) or len(bounds)!=2 or not finite(bounds[0])<=0<=finite(bounds[1]):raise ValueError('Invalid activation-relative range')
        finite(s['readback_tolerance'])
        if s['readback_tolerance']<0:raise ValueError('Negative readback tolerance')
        result[axis]=p
    return result


class RigHardware:
    """Only constructed by the owner, never in a card or Web worker.

    ``adopt_leases`` (owner restart with a running native lock, design §5, 2026-10-27): {resource key: {channel:
    lease token}} retained by the previous rig owner process on the BK/SDG owners; those resources are re-attached
    with ``RemoteAWG.adopt``/``RemoteServo.adopt`` (read-only verification) instead of a new claim."""
    def __init__(self, device, single_session=(), instrument_owners=None, registry=None, time_telemetry=None,
                 adopt_leases=None):
        from ldcon.drivers.ecdl import Servo, OffsetAWG, VisaAWG
        from ldcon.drivers.fizeau import HttpFizeauWavemeter
        self.device=device;self.connections={};self.locks=[]
        resources=device['connection']['resources']
        owners=instrument_owners or {}
        adopt_leases=dict(adopt_leases or {})
        try:
            for key,resource in resources.items():
                if 'owner' in resource:
                    if key in owners:raise DeviceError('Conflicting physical owner bindings for '+key)
                    if key!='tec_awg':raise DeviceError('Independent SDG adapter is qualified only for TEC DC offsets')
                    from ldcon.owners.sdg_client import RemoteOffsetAWG,axis_policy
                    retained=adopt_leases.get(key)
                    if retained:self.connections[key]=RemoteOffsetAWG.adopt(resource,next(iter(retained.values())))
                    else:self.connections[key]=RemoteOffsetAWG(resource,axis_policy(device,'temperature'))
                    continue
                host=resource['host'];port=80 if key=='piezo' else 5025
                if key in owners:
                    # Rule G (2026-10-22): the rig is a CLIENT of this instrument's owner. The owner holds the
                    # identity lock and the endpoint lock and the ONE session; the rig holds channel leases.
                    from ldcon.owners.remote_instruments import RemoteAWG,RemoteServo
                    spec=dict(owners[key]);spec.setdefault('channels',[resource['channel']] if key!='piezo' else None)
                    if key!='piezo':spec.setdefault('observe',list(resource.get('preserve_channels',[])))
                    name='ldcon rig '+device.get('name','')+key
                    retained=adopt_leases.get(key)
                    if retained:
                        if key=='piezo':
                            self.connections[key]=RemoteServo.adopt(spec,next(iter(retained.values())),registry=registry,owner=name,expected_host=host)
                        else:
                            self.connections[key]=RemoteAWG.adopt(spec,retained,registry=registry,owner=name,expected_host=host)
                        continue
                    self.connections[key]=(RemoteServo if key=='piezo' else RemoteAWG)(spec,registry=registry,owner=name,expected_host=host)
                    continue
                address=socket.gethostbyname(host)
                fingerprint=hashlib.sha256(f'{address}:{port}'.encode()).hexdigest()[:24]
                from ldcon.owners.migration_physical_lock import acquire_endpoint
                handle=acquire_endpoint(address,port)
                self.locks.append(handle)
                self.connections[key]=Servo(host) if key=='piezo' else VisaAWG(host) if key in single_session else OffsetAWG(host)
                if key=='piezo' and time_telemetry is not None:
                    # 2026-10-23: forward the ESP32 SNTP fields of the /status the rig already reads (report-only).
                    try:self.connections[key].status_observer=time_telemetry(address)
                    except Exception:pass
            self.identities={key:io.describe() for key,io in self.connections.items() if key!='piezo'}
            # 2026-10-22 SDG swap: the current AWG model follows its profile (owner-served: the instruments config's
            # profile, which the owner verifies at claim; direct connection: BK 4054B as before).
            from ldcon.drivers.ecdl import awg_profile,check_current_awg_identity
            self.current_profile=awg_profile(self.connections['current_awg'])
            check_current_awg_identity(self.current_profile,self.identities['current_awg'])
            expected=device['control_settings'].get('tec_idn_contains')
            if not expected or expected.upper() not in self.identities['tec_awg'].upper():raise DeviceError('TEC AWG model identity is not verified')
            wm=device['connection']['wavemeter']
            self.optical=HttpFizeauWavemeter(wm['endpoint'],channel=wm['channel'],cycle=wm['cycle'],
                trust_source_clock=wm['trust_source_clock'],expected_laser=wm['expected_laser'])
        except BaseException:self.close();raise
        # The raw transports (ecdl_projection.attach replaces ``connections`` values by guarded SystemConnections).
        self.transports=dict(self.connections)

    def lease_tokens(self):
        """{resource key: {channel: lease token}} this process holds on per-instrument owners (rule G); persisted by
        the rig owner so that the next owner process can adopt them (design §5)."""
        result={}
        for key,io in getattr(self,'transports',{}).items():
            leases=getattr(io,'leases',None)
            if isinstance(leases,dict) and leases:
                result[key]={str(ch):token for ch,token in leases.items()}       # RemoteAWG
            elif getattr(io,'lease',None) is not None and hasattr(io,'link'):
                result[key]={'0':io.lease}                                       # RemoteServo
            elif getattr(io,'lease',None) is not None and hasattr(io,'device_id'):
                result[key]={str(getattr(io,'channel',0)):io.lease}              # sdg_client.RemoteOffsetAWG
        return result

    def detach(self):
        """Deliberate owner shutdown with a running native lock: drop this process's transports and endpoint locks
        WITHOUT releasing the BK/SDG channel leases and without any instrument write (design §5, 2026-10-27)."""
        for io in getattr(self,'transports',self.connections).values():
            try:
                if hasattr(io,'detach'):io.detach()
                else:io.close()
            except Exception:pass
        for handle in self.locks:handle.close()

    def snapshot(self):
        servo=self.connections['piezo'];cfg=servo.get('config');status=servo.get('status')
        resources=self.device['connection']['resources']
        current=self.connections['current_awg'].read(resources['current_awg']['channel'])
        tec=self.connections['tec_awg'].read(resources['tec_awg']['channel'])
        return dict(outputs=dict(piezo=float(status['dac_val']),current=current['value'],temperature=tec['value']),
                    open_loop=all(cfg.get(k)==0 for k in ('pid_multiplier','multiplier_control','multiplier_target','software_modulation_enable')),
                    servo_config=cfg,servo_status=status,current=current,temperature=tec,identities=self.identities)

    def observation(self):
        from ldcon.owners.ecdl_optical import classify
        frame=self.optical.read_frame(allow_missing_frequency=True)
        self.last_optical_frame=frame
        config=self.device['control_settings']['optical']
        # v5.1-hostcheck: quality/aligned are informational labels (no host enforcement, human decision 2026-10-22).
        # The former current_recovery_source_states allow-list only existed to bypass the quality gate; the config
        # key is still accepted but inert.
        return dict(frequency=asdict(frame.frequency),**classify(frame,config))

    def reserved_readbacks(self):
        result={}
        for name in ('current_awg','tec_awg'):
            for ch in self.device['connection']['resources'][name].get('preserve_channels',[]):
                result[f'{name}/CH{ch}']=self.connections[name].read(ch)
        return result

    def optical_documentation(self):
        """Read the console's own static UI through the same owning process."""
        from urllib.request import build_opener,ProxyHandler
        url=self.device['connection']['wavemeter']['endpoint'].rstrip('/')+'/'
        with build_opener(ProxyHandler({})).open(url,timeout=5) as response:
            data=response.read(1024*1024+1)
        if len(data)>1024*1024:raise DeviceError('Optical console document exceeds size limit')
        return data.decode('utf-8')

    def optical_status(self):
        """Read console diagnostics; no calibration/resync/configuration calls."""
        from urllib.request import build_opener,ProxyHandler
        url=self.device['connection']['wavemeter']['endpoint'].rstrip('/')+'/api/status'
        with build_opener(ProxyHandler({})).open(url,timeout=5) as response:
            data=response.read(1024*1024+1)
        if len(data)>1024*1024:raise DeviceError('Optical console status exceeds size limit')
        return json.loads(data)

    def move(self, axis, value):
        if axis=='piezo':return self.connections['piezo'].set({'control_offset':value})
        name='current_awg' if axis=='current' else 'tec_awg'
        channel=self.device['connection']['resources'][name]['channel']
        return self.connections[name].offset(channel,value)

    def prepare(self, dac):
        return self.connections['piezo'].set(dict(control_offset=dac,pid_multiplier=0,
            multiplier_control=0,multiplier_target=0,software_modulation_enable=0))

    def configure_dither(self,settings,previous,*,paused=False,keep_bank=False):
        channel=self.device['connection']['resources']['current_awg']['channel']
        return self.connections['current_awg'].configure_dither(channel,settings['frequency_hz'],
            0. if paused else settings['effective_amplitude_peak']*2,previous,settings.get('programming'),
            paused=paused,keep_bank=keep_bank)

    def close(self):
        for io in self.connections.values():
            try:io.close()
            except Exception:pass
        for handle in self.locks:handle.close()


class ECDLOwner:
    def __init__(self, laser, *, audit_path, factory=RigHardware, clock=time.monotonic, laser_file=None, single_session_awgs=(),
                 instrument_owners=None, registry=None):
        from ldcon.core.interface_schema import validate_laser
        validate_laser(laser)
        # AWGs reached through ONE persistent VXI-11 session (shared with a broker channel; see shared_awg_session).
        self.single_session_awgs=tuple(single_session_awgs)
        # Rule G: resources served by per-instrument owners ({resource key: {instrument, endpoint?, channels?, observe?}}).
        self.instrument_owners=dict(instrument_owners or {})
        if self.instrument_owners and self.single_session_awgs and set(self.instrument_owners)&set(self.single_session_awgs):
            raise ValueError('A resource is either owner-served (instrument_owners) or a rig single session, not both')
        if factory is RigHardware:
            # 2026-10-23: the rig's own ESP32 /status reads also feed esp32_time_sync (laser telemetry endpoint).
            factory=lambda device,adopt_leases=None,_s=self.single_session_awgs,_o=self.instrument_owners,_r=registry:RigHardware(device,single_session=_s,instrument_owners=_o,registry=_r,time_telemetry=self._time_telemetry,adopt_leases=adopt_leases)
        self.device_key='rig';self.factory=factory;self.clock=clock
        self.laser_file=Path(laser_file) if laser_file else None
        self.lock=threading.RLock();self.hardware=None;self.lease=None;self.fault=None;self.activation=None
        # Set by the instrument service when one of its broker instruments shares a physical AWG with this rig:
        # (threading.Lock, callable returning the shared instruments' leases). See ecdl_locked_sweep.dispatch.
        self.shared_guard=None
        self.last_write={};self.recovery=None;self.last_recovery_frame=None;self.audit_path=Path(audit_path)
        self.audit_path.parent.mkdir(parents=True,exist_ok=True)
        from experiment_core.records import Journal
        from ldcon.owners.owner_projection import SystemPolicyAuthority
        from ldcon.owners.ecdl_projection import classify_policy
        self.policy_journal=Journal(self.audit_path.with_suffix('.authority.sqlite3'))
        dependencies=policy_dependencies(laser)
        migrated=migrate_policy_dependencies(self.policy_journal,laser,dependencies)
        if migrated is not None:self.audit('policy_dependencies_migrated',**migrated)
        # Rule M step 4 for the rig owner (2026-10-27, after the 09:04 and 20:25 start refusals "Saved authority
        # policy or dependencies differ"): the authority starts on the journal's SAVED policy; the yaml (layer 2) is
        # compared against it and adopted at the end of __init__ when the owner is idle (_adopt_startup). With a
        # persisted lock context (active lock to adopt, or a quarantine under review) a differing yaml refuses the
        # start with a precise message instead -- the running lock's conditions never change under it.
        from ldcon.owners.native_system_owner import saved_document
        from ldcon.core.agent_authority import describe_changes
        state=saved_document(self.policy_journal,laser['id']+'.policy',laser,dependencies)
        saved=state['value']['document']
        rows=describe_changes('lasers',saved,laser)
        self.requested_laser=copy.deepcopy(laser);self.requested_dependencies=dependencies
        self.startup=dict(diff=[r['path'] for r in rows],classes={r['path']:r['class'] for r in rows},
            dependencies_changed=state['dependencies']!=dependencies,
            saved_revision=digest(saved['devices']['rig']),requested_revision=digest(laser['devices']['rig']),
            journal_adoption_pending=state['pending'] is not None)
        self.startup['pending']=bool(rows) or self.startup['dependencies_changed']
        self.startup['state']='pending' if self.startup['pending'] else 'equal'
        self.policy_authority=SystemPolicyAuthority(laser['id'],saved,dependencies=state['dependencies'],
            journal=self.policy_journal,revision_function=digest,classify_change=classify_policy)
        # Rule M step 1: a dashboard save while a lease is held is adopted at the owner's own next safe boundary.
        self.adoption_request=None
        from ldcon.owners.ecdl_steady_owner import NativeLockOwner
        self.steady=NativeLockOwner(self)
        from ldcon.owners.emission_adapters import ecdl_emission
        self.emission=ecdl_emission(self)
        from ldcon.owners.operating_mode_owner import OperatingModeOwner
        self.operating_mode=OperatingModeOwner(self.policy_journal,laser['id'],
            coordinator=lambda:self.steady.background,interrupt=self._interrupt_background_mode)
        self.quarantine_path=self.audit_path.with_suffix('.quarantine.json')
        # Durable lock context (design §5 "owner 再起動とロックの再採用", 2026-10-27): lease, activation and the steady
        # lock state the next owner process needs to adopt a running native PI after a READ-ONLY verification.
        self.state_path=self.audit_path.with_suffix('.state.json')
        # A refused restart adoption is recorded here; <audit>.state.json keeps the ORIGINAL record (2026-10-27 20:36).
        self.refused_path=self.audit_path.with_suffix('.state.refused.json')
        self.adoption=None;self.shutdown_record=None;self._original_state=None
        persisted=self._persisted_lock_context()
        if self.startup['pending'] and persisted is not None:
            raise StaleContext(self._startup_refusal(persisted))
        if self.quarantine_path.exists():
            record=json.loads(self.quarantine_path.read_text())
            if record['device_revision']!=digest(self.device):raise DeviceError('Quarantined owner definition changed')
            original=self._retry_refused_adoption(record)
            if original is not None:
                self._restore_state(original)
            else:
                self.fault=record['fault'];self.lease=record['lease'];self.activation=record['activation']
                self.recovery=record.get('recovery');self.adoption=record.get('adoption')
                self.last_write={name:self.clock() for name in self.device['control_settings']['axes']}
                # A quarantine written while the native PI was armed used to refuse the start altogether ('requires
                # its original supervisor'). The supervisor process is gone either way: start faulted, touch nothing
                # (the firmware PI runs on inside its caps), keep the lease for the offline review (2026-10-27).
                self.audit('quarantine_restored',fault=self.fault,lease_retained=bool(self.lease),
                           native_armed=bool(record.get('native_armed')))
        elif self.state_path.exists():
            self._restore_state(json.loads(self.state_path.read_text()))
        if self.startup['pending']:self._adopt_startup()

    # ---- rule M step 4: idle start-up adoption of the laser yaml ----------------------------------------------------
    def _persisted_lock_context(self):
        """The lock context this start has to adopt or keep for review (None when the owner starts idle): a
        quarantine (fault, lease retained) or a durable record of an active native lock."""
        try:
            if self.quarantine_path.exists():
                record=json.loads(self.quarantine_path.read_text())
                return dict(kind='quarantine',lease=record.get('lease'),fault=record.get('fault'))
            if self.state_path.exists():
                record=json.loads(self.state_path.read_text())
                if record.get('active') and record.get('lease') and not record.get('fault'):
                    return dict(kind='active lock',lease=record['lease'],fault=None)
        except (OSError,ValueError,KeyError) as error:
            return dict(kind='unreadable state file',lease=None,fault=str(error))
        return None

    def _startup_refusal(self,persisted):
        s=self.startup
        changed=', '.join('%s (%s)'%(path,s['classes'].get(path,'?')) for path in s['diff']) or 'policy dependencies only'
        return ('Laser definition differs from the saved authority policy while this owner holds a %s (lease %s): '
                'changed %s; saved rig revision %s, yaml rig revision %s. The lock conditions are not changed under '
                'a persisted lock: restore the yaml to the saved policy and restart (the lock is adopted/reviewed '
                'unchanged), or adopt the change live through the dashboard (settings_adopt) before restarting, '
                'or release the lease after the review (the next idle start adopts the yaml).'
                %(persisted['kind'],persisted.get('lease'),changed,s['saved_revision'],s['requested_revision']))

    def _adopt_startup(self):
        """Idle start (no lease, no fault, no persisted lock): adopt the yaml as the new saved authority policy,
        dependencies included (connection/calibration changes are the explicit idle configuration transition), with
        a journaled startup receipt and the audit record ``policy_adopted_at_start``. No hardware I/O."""
        s=self.startup
        if self.lease is not None or self.fault or self.adoption is not None:
            s['state']='pending'
            self.audit('policy_yaml_pending_at_start',changed_paths=s['diff'],reason='owner is not idle')
            return
        if s['journal_adoption_pending']:
            s['state']='pending'
            self.audit('policy_yaml_pending_at_start',changed_paths=s['diff'],reason='a journal adoption is pending reconciliation')
            return
        if s['dependencies_changed']:
            self.policy_authority.configuration.set_dependencies(copy.deepcopy(self.requested_dependencies))
        if s['diff']:
            from ldcon.owners.ecdl_policy_reload import commit
            receipt=dict(human_identity=dict(source='laser definition adopted at owner start',kind='rig',
                                             changed_paths=list(s['diff'])),
                         receipt_id='startup:rig:'+s['requested_revision'])
            commit(self,self.requested_laser,receipt,None)
        s.update(state='adopted',pending=False)
        self.audit('policy_adopted_at_start',changed_paths=s['diff'],classes=s['classes'],
                   saved_revision=s['saved_revision'],effective_revision=digest(self.device),
                   dependencies_changed=s['dependencies_changed'])

    # ---- design §5: restart adoption of a persisted active lock ---------------------------------------------------
    def _retry_refused_adoption(self,quarantine):
        """A restart adoption refused for a TRANSIENT reason (TypeError = owner-side bug, OSError/BrokerError = rig
        or SDG/BK owner unreachable until the deadline) is re-attempted at the next start from the ORIGINAL durable
        record, which the refusal left untouched (2026-10-27 20:36: the refusal used to overwrite it with
        ``steady: None``, making a second attempt impossible). A definitive refusal (the rig differs: PI off,
        configuration/firmware changed, window, dither, in-flight operation, revision) is restored as a quarantine
        as before. Returns the original record to adopt, or None."""
        a=quarantine.get('adoption') or {}
        if a.get('state')!='refused' or not a.get('transient') or not self.state_path.exists():return None
        try:record=json.loads(self.state_path.read_text())
        except (OSError,ValueError):return None
        if not (record.get('active') and record.get('lease') and record['lease']==a.get('lease') and not record.get('fault')):
            return None
        self.audit('owner_restart_adoption_retried',lease=record['lease'],previous_reason=a.get('reason'),
                   refused_unix_s=a.get('refused_unix_s'))
        from ldcon.core.records import atomic_json
        atomic_json(self.quarantine_path.with_name('adoption-retried-quarantine-%s.json'%time.strftime('%Y%m%dT%H%M%S')),quarantine)
        self.quarantine_path.unlink()
        return record

    def _restore_state(self,record):
        """Startup with a persisted active lock: restore the context and adopt the same lease read-only (design §5)."""
        if not (record.get('active') and record.get('lease') and not record.get('fault')):
            return  # a lease without an armed native lock is not persisted (the next owner starts clean, as before)
        self._original_state=copy.deepcopy(record)
        self.lease=record['lease'];self.activation=record.get('activation');self.recovery=None
        self.shutdown_record=record.get('shutdown')
        self.adoption=dict(state='pending',started=self.clock(),started_unix_s=time.time(),lease=self.lease,attempts=0,
                           remote_leases=record.get('remote_leases') or None,previous_shutdown=record.get('shutdown'))
        steady=record.get('steady') or {}
        self.steady.restore(steady)
        self.adoption['deadline']=self.adoption['started']+float((steady.get('profile') or {}).get('watchdog_s') or 90.)
        if record.get('device_revision')!=digest(self.device):
            self._refuse_adoption('rig definition revision changed while the native lock ran (%s != %s)'
                                  %(record.get('device_revision'),digest(self.device)))
            return
        in_flight=[r for r in steady.get('in_flight') or []]
        if in_flight:
            self._refuse_adoption('an owner operation was in flight at shutdown: '+', '.join(in_flight))
            return
        self.audit('owner_restart_adoption_started',lease=self.lease,previous_shutdown=self.shutdown_record)
        self.adopt_tick()
        if self.adoption['state']=='pending':
            self.steady.start_watch()   # retries the read-only adoption until the deadline

    def adopt_tick(self):
        """One read-only adoption attempt of the persisted active lock (called at startup and from the watchdog thread
        while pending). Transport failures keep the adoption pending until its deadline; a firmware/rig state that
        differs from the persisted expectation refuses it with a precise reason. Never writes."""
        with self.lock:
            a=self.adoption
            if not a or a['state']!='pending':return a
            a['attempts']+=1
            try:
                if self.hardware is None:self.hardware=self.open_hardware(adopt_leases=a.get('remote_leases'))
                if self.fault:   # ecdl_projection.attach found a physical writer that needs reconciliation
                    self._refuse_adoption(self.fault);return a
                state=self.hardware.snapshot()
            except (OSError,BrokerError) as error:
                a['last_error']=str(error)
                if self.clock()>a['deadline']:
                    self._refuse_adoption('rig state unreadable for %.0f s: %s'%(self.clock()-a['started'],error),transient=True)
                return a
            except (DeviceError,ValueError,TypeError,KeyError) as error:
                # TypeError is an owner-side defect (2026-10-27 20:27: the factory refused adopt_leases), not a rig
                # that differs: transient, re-attempted from the original record at the next start.
                self._refuse_adoption(str(error),transient=isinstance(error,TypeError));return a
            try:
                policies=self.policies()
                self.validate_state(state,policies)
                for axis in policies:
                    bounds=self.device['absolute_conditions']['axes'][axis]['activation_range']
                    value=state['outputs'][axis];origin=finite(self.activation[axis],axis)
                    if not origin+bounds[0]<=value<=origin+bounds[1]:
                        raise DeviceError('%s output %.6g is outside the activation-relative window [%.6g, %.6g]'
                                          %(axis,value,origin+bounds[0],origin+bounds[1]))
                self.steady.adopt_verify(state)
            except (DeviceError,ValueError,TypeError,KeyError) as error:
                self._refuse_adoption(str(error),transient=isinstance(error,TypeError));return a
            now=self.clock()
            self.last_write={axis:now for axis in policies}
            self.steady.adopt(state,policies)
            a.update(state='adopted',adopted_unix_s=time.time(),heartbeat_grace_s=self.steady.p['watchdog_s'],
                     pid_multiplier=state['servo_config'].get('pid_multiplier'),dac=state['outputs']['piezo'])
            self.audit('owner_restart_adopted',lease=self.lease,attempts=a['attempts'],outputs=state['outputs'],
                       heartbeat_grace_s=a['heartbeat_grace_s'])
            self.persist_state()
            return a

    def _refuse_adoption(self,reason,*,transient=False):
        """The persisted active lease cannot be adopted: THIS is the uncertain state. No firmware write; the lease and
        the retained BK/SDG leases stay for the offline review (quarantine). The ORIGINAL durable record stays on
        disk untouched (it is the only copy of the steady context the retry or the review needs); the refusal is
        written to the sibling ``<audit>.state.refused.json``. ``transient`` (TypeError/OSError/BrokerError) makes
        the next start re-attempt the adoption from that record (``_retry_refused_adoption``)."""
        self.adoption.update(state='refused',reason=str(reason),refused_unix_s=time.time(),transient=bool(transient))
        self.steady.stop_event.set();self.steady.active=False;self.steady.armed=False
        self.fault='Owner restart adoption refused: '+str(reason)+'; native PI not touched, leases retained; offline review required'
        self.audit('owner_restart_adoption_refused',lease=self.lease,reason=str(reason),transient=bool(transient))
        self.persist_fault()
        from experiment_core.records import atomic_json
        try:
            atomic_json(self.refused_path,json.loads(json.dumps(dict(schema_version=1,refused_unix_s=time.time(),
                reason=str(reason),transient=bool(transient),fault=self.fault,lease=self.lease,
                adoption=self.adoption,original=self._original_state),default=_jsonable,allow_nan=False)))
        except (OSError,ValueError,TypeError) as error:self.audit('state_persist_failed',error=str(error))

    def persist_state(self,shutdown=None):
        """Durable lock context for the next owner process (written at claim/arm/hold/release, after every firmware
        write and at a deliberate shutdown). JSON only; no secrets."""
        from experiment_core.records import atomic_json
        if (self.adoption or {}).get('state')=='refused':
            # A refused adoption never overwrites the original record (the refusal is in <audit>.state.refused.json
            # and the quarantine); release clears both.
            return
        hardware=self.hardware
        if hardware is not None and hasattr(hardware,'lease_tokens'):remote=hardware.lease_tokens()
        else:remote=(self.adoption or {}).get('remote_leases')   # not reopened yet: keep the tokens for the review
        if shutdown is not None:self.shutdown_record=shutdown
        record=dict(schema_version=1,device_revision=digest(self.device),lease=self.lease,
            activation=self.activation,fault=self.fault,active=bool(self.steady.armed and self.steady.active),
            steady=self.steady.durable(),remote_leases=remote or None,adoption=self.adoption,
            shutdown=self.shutdown_record,written_unix_s=time.time())
        atomic_json(self.state_path,json.loads(json.dumps(record,default=_jsonable,allow_nan=False)))

    def clear_state(self):
        self.adoption=None;self.shutdown_record=None;self._original_state=None
        for path in (self.state_path,self.refused_path):
            try:path.unlink()
            except FileNotFoundError:pass

    # warning_max is accepted only for audited removal of the retired block.
    HOT_OPTICAL=('single_mode_max','classification_enabled','warning_max')
    # Read-only lease-holder requests between which a deferred settings_adopt is applied (rule M step 1).
    ADOPTION_BOUNDARIES=frozenset(('steady_heartbeat','steady_read','read','observe'))

    def _interrupt_background_mode(self,reason):
        self.steady.clear_target_response('operating mode: '+str(reason))
        background=self.steady.background
        background.generation+=1
        background.returning(reason)
        if background.epoch and background.epoch.get('family')=='pi':
            from ldcon.owners.ecdl_background_pid import request_return,restore_atomic
            if background.epoch.get('completion_mode')=='verified_handover':restore_atomic(self.steady,reason)
            else:request_return(self.steady)
        self.steady.discard_proposals(reason)
        self.audit('operating_mode_changed',operating_mode=self.operating_mode.authority.status())

    @property
    def laser(self):
        return self.policy_authority.snapshot()

    @property
    def device(self):
        return self.laser['devices'][self.device_key]

    def open_hardware(self,adopt_leases=None):
        from ldcon.owners.ecdl_projection import attach
        if adopt_leases:return attach(self,self.factory(self.device,adopt_leases=adopt_leases))
        return attach(self,self.factory(self.device))

    def reload_settings(self, request):
        from ldcon.owners.ecdl_policy_reload import reload_settings
        with self.lock:
            return reload_settings(self, request)

    def bind_shared_awg(self,name,authority,constraints,channels):
        """Trusted host startup: bind a sibling system to the existing session."""
        from ldcon.owners.ecdl_projection import SystemConnection
        with self.lock:
            if name not in self.single_session_awgs:
                raise DeviceError('A shared binding requires one persistent physical session')
            physical=self.hw().connections[name]._physical
            resources=['CH'+str(channel) for channel in channels]
            binding=physical.guard.bind(authority,resources)
            connection=SystemConnection(physical,authority,binding=binding)
            connection.install_constraints(constraints)
            self.__dict__.setdefault('_shared_connections',{}).setdefault(name,[]).append(connection)
            return connection

    def shared_awg_session(self, name, operations, *, connection=None):
        """Broker operations for another owner's channel of this rig's AWG, on the rig's ONE persistent VXI-11 session
        under the rig lock (no second LAN session, no reconnects). ``operations`` = [(op, data)], op in
        query/write/write_raw; channel addressing was already restricted by the broker. Any I/O error leaves the
        shared session state unknown for both users: the rig faults (quarantine) and the error propagates."""
        from ldcon.drivers.ecdl import VisaAWG
        with self.lock:
            hw=self.hw()
            awg=hw.connections.get(name)
            if name not in self.single_session_awgs:
                raise DeviceError('Routed broker access requires the rig\'s single persistent session for '+name)
            if connection is not None:
                if connection not in self.__dict__.get('_shared_connections',{}).get(name,[]):
                    raise DeviceError('Shared transport requires its configured system binding')
                awg=connection
            results=[]
            try:
                for op,data in operations:
                    if op=='query':results.append(awg.query(data))
                    elif op=='write':awg._send(data);results.append(None)
                    elif op=='write_raw':awg.write_raw(data);results.append(None)
                    else:raise DeviceError('Unsupported routed operation '+str(op))
            except (AuthorizationRequired,StaleContext):
                # The physical guard refused before its durable write intent;
                # a foreign scope or stale grant does not fault another laser.
                raise
            except Exception as exc:
                self.fault='Owner I/O requires review: shared AWG session failed: '+str(exc)
                self.persist_fault()
                raise DeviceError(self.fault) from exc
            self.audit('shared_awg_session',awg=name,operations=[op for op,_ in operations])
            return results

    def persist_fault(self):
        if not self.fault:return
        self.steady.clear_target_response('owner fault')
        from ldcon.core.records import atomic_json
        existing=json.loads(self.quarantine_path.read_text()) if self.quarantine_path.exists() else {}
        atomic_json(self.quarantine_path,{**existing,'fault':self.fault,'lease':self.lease,
            'activation':self.activation,'recovery':self.recovery,'device_revision':digest(self.device),
            'native_armed':self.steady.armed,'adoption':self.adoption})

    def review_dither(self):
        """Adopt an acknowledged dither after read-only review; never retry it."""
        from ldcon.drivers.ecdl import arb_name_matches
        from ldcon.core.records import atomic_json
        record=json.loads(self.quarantine_path.read_text())
        transport_review=(record.get('review_kind')=='native_transport_pre_io_failure'
            and record.get('pre_io_verified') is True and self.fault
            and "module 'websocket' has no attribute 'create_connection'" in self.fault)
        dither_review=(record.get('review_kind')=='dither_readback_mismatch' and self.fault
            and self.fault.startswith('Uncertain dither configuration:'))
        if not (transport_review or dither_review) or self.steady.armed or record.get('native_armed'):
            raise DeviceError('Only the documented unarmed configuration may be reviewed')
        context=record['dither'];before=context['before'];programmed=context['programmed']
        if self.hardware is None:self.hardware=self.open_hardware()
        state=self.hardware.snapshot();self.validate_state(state,self.policies())
        if not state['open_loop'] or state['outputs']!=before['outputs'] or state['temperature']!=before['temperature']:
            raise DeviceError('Held outputs changed; dither review cannot clear the fault')
        if transport_review and state['servo_config']!=before['servo_config']:
            raise DeviceError('Native configuration changed; transport review cannot clear the fault')
        current=state['current']
        if (not arb_name_matches(current['arb_name'],programmed['name'])
                or abs(current['frequency_hz']-programmed['frequency_hz'])>1e-6):
            raise DeviceError('Dither readback still differs from the acknowledged request')
        if any(current[k]!=v for k,v in before['current'].items() if k not in ('arb_name','frequency_hz')):
            raise DeviceError('Current output configuration changed during dither review')
        if self.hardware.reserved_readbacks()!=context['reserved']:
            raise DeviceError('Reserved channels changed; dither review cannot clear the fault')
        path=self.audit_path.parent/('dither-reviewed-'+self.lease+'.json')
        result=dict(settings=context['settings'],programmed=programmed,before=before,after=state,
                    reserved=context['reserved'],evidence=str(path),reviewed_fault=self.fault)
        atomic_json(path,result)
        self.audit('dither_review_verified',**result)
        from ldcon.owners.ecdl_projection import reconcile_reviewed
        reconcile_reviewed(self,result)
        atomic_json(self.quarantine_path.with_name('dither-reviewed-quarantine-'+self.lease+'.json'),record)
        self.quarantine_path.unlink()
        self.steady.programmed_dither=result;self.steady.qualification=[];self.steady.qualification_context=None
        self.fault=None
        return result


    def audit(self, kind, **values):
        fd=os.open(self.audit_path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as f:
            f.write(json.dumps(dict(kind=kind,unix_s=time.time(),**values),allow_nan=False)+'\n')
            f.flush();os.fsync(f.fileno())

    def _time_telemetry(self, address):
        # One exporter (one UDP socket) per ESP32 address for the owner's lifetime: hardware reopens reuse it.
        cache=self.__dict__.setdefault('_time_exporters',{})
        if address not in cache:
            from ldcon.owners.esp32_time_telemetry import for_laser
            cache[address]=for_laser(self.laser,host=address,device='esp32servocon-'+str(self.laser['id']),producer='rig-owner')
        return cache[address]

    def hw(self):
        if self.fault:raise DeviceError('Owner fault requires review: '+self.fault)
        if self.adoption is not None and self.adoption.get('state')=='pending':
            # No fresh claim on the BK/SDG owners and no write while the persisted lock is still being adopted.
            raise MeasurementUnavailable('Owner restart adoption pending (attempt %d): %s'
                                         %(self.adoption['attempts'],self.adoption.get('last_error','rig not yet verified')))
        if self.hardware is None:
            self.hardware=self.open_hardware()
            from ldcon.owners.ecdl_locked_sweep import recover_adc_log
            recover_adc_log(self)   # 2026-10-23: a temporary ADC log threshold left by a previous owner (never raises)
        # inspect may have opened a read-only connection before the first claim.
        from ldcon.owners.ecdl_locked_sweep import recover_full_data_log
        recover_full_data_log(self)
        return self.hardware

    def policies(self):
        return axis_policies(self.device)

    def compensation_policy(self, request, state, policy):
        """Fast manual-knob cancellation only; ordinary tuning keeps its limits."""
        config=self.device['control_settings'].get('temperature_compensation',{})
        if config.get('enabled') is not True or request.get('axis')!='temperature':
            raise BrokerError('configuration','Manual TEC compensation is not enabled for this axis')
        lead=request.get('lead_delta_v')
        if lead is not None:
            if request.get('knob_delta_ohm') is not None or request.get('voltage_lead_requested') is not True:
                raise BrokerError('configuration','Voltage lead requires an explicit request, not a reported knob move')
            if type(lead) not in (int,float) or not math.isfinite(lead) or not 0<lead<=.05:
                raise BrokerError('range','Voltage lead must be positive and at most50mV')
            delta=-lead*finite(config['ohm_per_v'])
        else:delta=request.get('knob_delta_ohm')
        if type(delta) not in (int,float) or not math.isfinite(delta) or delta==0:
            raise BrokerError('configuration','Finite nonzero reported knob change is required')
        gain=finite(config['ohm_per_v']);interval=finite(config['minimum_interval_s'])
        if gain<=0 or interval<=0:raise BrokerError('configuration','Invalid compensation calibration')
        for axis in ('piezo','current'):
            tolerance=self.device['control_settings']['axes'][axis]['readback_tolerance']
            if abs(state['outputs'][axis]-self.activation[axis])>tolerance:
                raise BrokerError('configuration','Other axes changed during manual compensation')
        start=self.activation['temperature'];end=start-delta/gain
        bounds=self.device['absolute_conditions']['axes']['temperature']['activation_range']
        low=max(policy.low,start+bounds[0]);high=min(policy.high,start+bounds[1])
        value=finite(request['value']);previous=state['outputs']['temperature']
        if not low<=end<=high or not min(start,end)-1e-12<=value<=max(start,end)+1e-12:
            raise BrokerError('range','Compensation must stay on the reported knob cancellation path')
        if (value-previous)*(end-start)<-1e-12:
            raise BrokerError('range','Compensation must progress toward cancellation')
        if state.get('open_loop') is not True or self.steady.armed:
            raise BrokerError('ownership','Manual compensation requires an open unarmed loop')
        self.audit('temperature_compensation_requested',knob_delta_ohm=delta if lead is None else None,lead_delta_v=lead,
                   origin=start,endpoint=end,value=value)
        from ldcon.owners.tec_rate import rate_only,delivery_step
        if rate_only(self.device):
            return replace(policy,update_interval_s=interval,
                           max_step=delivery_step(policy.max_slew_per_s,interval))
        return replace(policy,update_interval_s=interval,
                       max_slew_per_s=policy.max_step/interval)

    def observation(self, spectral_only=False):
        hardware=self.hw()
        try:result=hardware.observation()
        except DeviceError as error:raise MeasurementUnavailable(str(error)) from error
        sample=result['frequency'];now=self.clock()
        max_age=self.device['control_settings']['max_sample_age_s']
        # v5.1-hostcheck (human decision 2026-10-22): a finite reading from the current cycle is enough. The
        # wavemeter quality/status and the aligned/acquisition_known flags are labels only. acquired_s is the
        # source-clock acquisition time when trusted+aligned, otherwise the host receive time (fizeau.read_frame),
        # so the age test below always measures "current cycle".
        valid_frequency=(isinstance(sample['frequency_hz'],(int,float))
                         and math.isfinite(sample['frequency_hz']) and sample['frequency_hz']>0)
        age=now-sample['acquired_s'] if isinstance(sample.get('acquired_s'),(int,float)) else None
        if ((not valid_frequency and not spectral_only) or sample['unit']!='Hz'
                or age is None or not 0<=age<=max_age):
            raise MeasurementUnavailable('A finite optical reading from the current cycle is required')
        return result

    def validate_state(self, state, policies):
        for name,p in policies.items():
            value=finite(state['outputs'][name],name)
            if not p.low<=value<=p.high:raise DeviceError(name+' is outside the approved envelope')
            if name=='piezo':continue
            channel=state[name]
            if not channel['enabled'] or channel['load']!='HZ' or channel['polarity']!='NOR':raise DeviceError('AWG output/load/polarity is not ready')
            if channel['burst']!='OFF' or channel['modulation']!='OFF' or channel['sweep']!='OFF':raise DeviceError('AWG is armed or modulated externally')
            allowed=('DC',) if name=='temperature' else ('DC','SINE','ARB')
            if channel['waveform'] not in allowed:raise DeviceError('Unsupported prelock waveform')
            half=finite(channel['amplitude_vpp'])/2
            if half<0 or value-half<p.low or value+half>p.high:raise DeviceError('Analog modulation exceeds approved envelope')

    def dispatch(self, request):
        try:
            with self.emission.store.cleanup_scope(request.get('action')):
                from ldcon.owners.operating_mode_owner import MODE_ACTIONS
                if request.get('action') in MODE_ACTIONS:
                    if request['action']=='operating_mode_status':
                        return self.operating_mode.dispatch(request)
                    with self.lock:
                        return self.operating_mode.dispatch(request)
                with self.lock:
                    self.operating_mode.before(request)
                    result=self._dispatch(request)
                    return self.operating_mode.after(request,result)
        except BrokerError:raise
        except OperationError as error:
            if isinstance(error,UncertainWrite):
                self.fault='Physical projection requires reconciliation: '+str(error)
                self.persist_fault()
            raise BrokerError('fault' if self.fault else 'configuration',str(error)) from error
        except MeasurementUnavailable as error:
            if self.fault:
                self.persist_fault()
                raise BrokerError('fault',str(error)) from error
            self.audit('measurement_rejected',action=request.get('action'),reason=str(error))
            raise BrokerError('measurement',str(error)) from error
        except OSError as error:
            self.fault='Owner I/O requires review: '+str(error)
            self.persist_fault()
            raise BrokerError('io',str(error)) from error
        except (DeviceError,ValueError,TypeError,KeyError) as error:
            self.persist_fault()
            if request.get('action') in ('move','prepare'):
                self.audit('request_rejected',axis=request.get('axis'),value=request.get('value'),reason=str(error),uncertain=bool(self.fault))
            raise BrokerError('fault' if self.fault else 'range',str(error)) from error

    def _dispatch(self, request):
        if request.get('action') in ('emission_status','emission_cancel'):
            return self.emission.dispatch(request)
        if request.get('action')=='status':
            sweep=self.steady.sweep
            emission=self.emission.dispatch({'action':'emission_status'})
            # Status never waits on a held lock (emission changes); the study scope
            # sync and its report happen whenever the lock is free.
            study=None
            if self.lock.acquire(blocking=False):
                try:study=self.steady.target_ff_study_status()
                finally:self.lock.release()
            return dict(simulated=False,rig=self.laser['id'],connected=self.hardware is not None,
                busy=self.lease is not None or emission['phase'] in ('quiescing','executing','changing','verifying','unknown'),emission=emission,fault=self.fault,writes_enabled=self.laser['allow_output'],
                locked_sweep=None if sweep is None else dict(state=sweep.state,request_id=sweep.plan['request_id']),
                native_input=self.steady.native_input_fields(),  # report-only (v5.1-hostcheck)
                target_ff_study=study,native_pi_active=bool(self.steady.active),
                adoption=copy.deepcopy(self.adoption),last_shutdown=copy.deepcopy(self.shutdown_record),
                esp32_time_telemetry={a:e.counters() for a,e in self.__dict__.get('_time_exporters',{}).items() if e is not None})  # report-only (2026-10-23)
        with self.lock:
            action=request['action']
            if isinstance(action,str) and action.startswith('emission_'):
                return self.emission.dispatch(request)
            self.emission.store.require_control(action)
            if action=='describe':
                from ldcon.owners.ecdl_policy_reload import policy_state
                adoption=policy_state(self)
                try:policies={k:asdict(v) for k,v in self.policies().items()}
                except (ValueError,TypeError,KeyError):policies=None
                # The adopted runtime-adjustment envelope (same computation as background_status), so a lease-less
                # reader (the agent background gateway) never reports a job's frozen laser copy (2026-10-28).
                from ldcon.owners.ecdl_background_pid import capabilities as background_capabilities
                return dict(laser_id=self.laser['id'],revision=digest(self.device),policy_adoption=adoption,policies=policies,
                            background_capabilities=background_capabilities(self.steady),
                            policy_startup=copy.deepcopy(self.startup),
                            writes_enabled=self.laser['allow_output'],fault=self.fault,
                            capabilities=dict(locked_sweep=1,operating_point=1,lock_target=1,steady_runtime_parameters=1,emission=1),
                            emission=self.emission.dispatch({'action':'emission_status'}))
            if action=='inspect':
                # Read-only commissioning is available even before limits are supplied.
                # Diagnostic reads must remain possible without clearing faults.
                # Reopening a quarantined, unarmed transport performs no writes.
                if self.adoption is not None and self.adoption.get('state')=='pending':
                    self.adopt_tick()   # a human inspect also retries the read-only adoption
                    if self.adoption['state']=='pending':
                        raise MeasurementUnavailable('Owner restart adoption pending: '+str(self.adoption.get('last_error')))
                if self.hardware is None:self.hardware=self.open_hardware()
                hw=self.hardware
                self.steady.sync_target_response()
                result=dict(fault=self.fault,lease_retained=bool(self.lease),snapshot=hw.snapshot(),
                            reserved_readbacks=hw.reserved_readbacks() if hasattr(hw,'reserved_readbacks') else {},
                            native_pi_active=bool(self.steady.active),adoption=copy.deepcopy(self.adoption),
                            last_shutdown=copy.deepcopy(self.shutdown_record))
                result['dither']=self.steady.dither_fields()
                result['tec_context']=self.steady.tec_context_fields()
                result['target_ff_study']=self.steady.target_ff_study_status()
                result['background']=self.steady.background_dispatch({'action':'background_status'})
                try:result['observation']=hw.observation()
                except DeviceError as error:result['observation_error']=str(error)
                result['source_metadata']=getattr(getattr(hw,'optical',None),'last_source_metadata',{})
                if request.get('optical_capture') is True and hasattr(hw,'last_optical_frame'):result['optical_capture']=asdict(hw.last_optical_frame)
                if request.get('optical_documentation') is True:result['optical_documentation']=hw.optical_documentation()
                if request.get('optical_status') is True:result['optical_status']=hw.optical_status()
                return result
            if action=='claim':
                if self.lease is not None:raise BrokerError('busy',self.laser['id']+' already has an owning card')
                from ldcon.owners.ecdl_policy_reload import adopt_saved
                adopt_saved(self,request.get('revision'))
                if request.get('revision')!=digest(self.device):raise BrokerError('configuration','Frozen device definition differs from owner')
                if not self.laser['allow_output']:raise BrokerError('range',self.laser['id']+' writes await approved safety settings')
                policies=self.policies();state=self.hw().snapshot()
                if self.fault:raise BrokerError('fault','Physical owner requires readback reconciliation before a new lease')
                try:self.validate_state(state,policies)
                except DeviceError:
                    # v5.2-hostcheck: the BK still holds THIS owner's locked-sweep state (armed burst / ldcsw records):
                    # the supported DC-first cleanup restores the reviewed rest, then the claim is checked again.
                    from ldcon.owners.ecdl_locked_sweep import locked_sweep_leftover,cleanup_awgs,active
                    if self.fault or (getattr(self,'steady',None) is not None and active(self.steady)) or not locked_sweep_leftover(self):raise
                    self.audit('claim_locked_sweep_cleanup',reason='AWG holds this owner locked-sweep records')
                    cleanup_awgs(self)
                    state=self.hw().snapshot();self.validate_state(state,policies)
                recovery_only=request.get('current_recovery') is True
                if recovery_only and not state['open_loop']:raise DeviceError('Recovery-only claim requires an already open servo')
                self.observation(spectral_only=recovery_only)
                token=uuid.uuid4().hex
                self.audit('claim',owner=str(request.get('owner','card')),lease=token,activation=state['outputs'])
                self.lease=token;self.activation=dict(state['outputs'])
                self.last_write={k:self.clock() for k in policies}
                self.recovery=dict(state['outputs']) if recovery_only else None
                self.last_recovery_frame=None
                self.adoption=None;self.shutdown_record=None
                self.persist_state()
                effective={}
                for k,p in policies.items():
                    bounds=self.device['absolute_conditions']['axes'][k]['activation_range']
                    half=0. if k=='piezo' else state[k]['amplitude_vpp']/2
                    effective[k]=asdict(replace(p,low=max(p.low,self.activation[k]+bounds[0])+half,
                        high=min(p.high,self.activation[k]+bounds[1])-half))
                return dict(lease=self.lease,policies=effective)
            if action=='settings_adopt':
                # Rule M step 1: lease-less entry for the dashboard save. No lease: the saved definition is adopted
                # now (same gates as the next claim); a held lease: deferred to this owner's next safe boundary.
                from ldcon.owners.ecdl_policy_reload import settings_adopt
                return settings_adopt(self,request)
            if self.lease is None or request.get('lease')!=self.lease:raise BrokerError('ownership','Exclusive rig lease required')
            if action in self.ADOPTION_BOUNDARIES and self.adoption_request is not None:
                from ldcon.owners.ecdl_policy_reload import adopt_deferred
                adopt_deferred(self,action)
            if action=='review_dither':return self.review_dither()
            if action=='settings_reload':
                self.steady.clear_target_response('foreground settings reload')
                return self.reload_settings(request)
            if action=='release':
                if self.fault:raise BrokerError('fault','Uncertain output: rig lease is retained for review')
                if self.steady.armed:raise BrokerError('ownership','Native feedback must be held before releasing its supervisor lease')
                self.audit('release');self.lease=None;self.activation=None;self.recovery=None
                self.clear_state();return True
            from ldcon.owners.ecdl_steady_owner import ACTIONS
            if action=='steady_heartbeat' and self.fault:
                # The worker learns the precise reason (e.g. a refused restart adoption), not 'lock is not active'.
                raise BrokerError('fault','Owner fault requires review: '+self.fault)
            if action in ACTIONS:return self.steady.dispatch(request)
            if action=='validate_external_sweep':
                from ldcon.owners.external_sweep_guard import validate_servo_sweep
                return validate_servo_sweep(self,request)
            if action=='read':return self.hw().snapshot()
            if action=='observe':
                result=self.observation(spectral_only=self.recovery is not None)
                if request.get('include_patterns') is True:
                    frame=getattr(self.hw(),'last_optical_frame',None)
                    if frame is None:raise MeasurementUnavailable('Raw optical frame is unavailable')
                    optical=self.device['control_settings']['optical']
                    result['fringe_analysis']={**(result.get('fringe_analysis') or {}),
                        'raw_patterns':[asdict(pattern) for pattern in frame.patterns],
                        'classification_method':optical.get('metric_method','unconfigured'),
                        'classification_revision':digest(optical),
                        'classification_enabled':optical.get('classification_enabled',False)}
                self.audit('optical_observation',**result);return result
            if action in ('begin_current_recovery','end_current_recovery'):
                if self.steady.armed:raise DeviceError('Hold native feedback before entering current recovery')
                state=self.hw().snapshot();self.validate_state(state,self.policies())
                if not state['open_loop']:raise DeviceError('Current mapping requires open loop')
                self.observation(spectral_only=action=='begin_current_recovery')
                self.audit(action,outputs=state['outputs'])
                self.recovery=dict(state['outputs']) if action=='begin_current_recovery' else None
                self.last_recovery_frame=None
                return True
            if action not in ('move','prepare','recover_current','locked_move','compensate_temperature'):raise BrokerError('action','Only typed ECDL operations are supported')
            if action=='locked_move' and not getattr(self.steady,'adjusting',False):raise DeviceError('Locked moves require an owner-generated supervision action')
            if self.steady.armed and action!='locked_move':raise DeviceError('Native lock owns the rig; use stationary lifecycle operations')
            if not self.laser['allow_output']:raise BrokerError('range',self.laser['id']+' output writes are disabled')
            policies=self.policies()
            if action=='locked_move':state,locked_optical=self.steady.read_feedback()
            else:state=self.hw().snapshot()
            self.validate_state(state,policies)
            recovery_move=action=='recover_current'
            if recovery_move:
                if self.recovery is None or request.get('axis')!='current':raise DeviceError('Explicit current recovery state required')
                for name in ('piezo','temperature'):
                    if abs(state['outputs'][name]-self.recovery[name])>self.device['control_settings']['axes'][name]['readback_tolerance']:
                        raise DeviceError('Frozen axis changed during current mapping')
            elif self.recovery is not None:
                raise DeviceError('Normal output commands are blocked during current recovery')
            optical=locked_optical if action=='locked_move' else self.observation(spectral_only=recovery_move)
            frame=optical['frequency'].get('acquired_unix_s')
            if recovery_move and frame is not None and self.last_recovery_frame is not None and frame<=self.last_recovery_frame:
                raise MeasurementUnavailable('Repeated optical acquisition before current recovery write')
            if action=='prepare':
                if state['open_loop']:return state
                # Preserve the present DAC in the opening request. Firmware
                # multi-key updates are not assumed to be atomic.
                value=state['outputs']['piezo']
                self.audit('prepare_requested',dac=value,config=state['servo_config'])
                try:
                    self.hw().prepare(value);after=self.hw().snapshot()
                    if not after['open_loop'] or abs(after['outputs']['piezo']-value)>1.:raise DeviceError('Open-loop handover readback mismatch')
                except Exception as error:
                    self.fault='Uncertain open-loop handover: '+str(error);raise
                self.audit('prepare_applied',outputs=after['outputs']);return after
            if not state['open_loop'] and action!='locked_move':raise DeviceError('Output command requires an open-loop servo')
            axis=request['axis']
            if axis not in policies:raise BrokerError('axis','Unknown ECDL axis')
            p=policies[axis]
            if action=='compensate_temperature':p=self.compensation_policy(request,state,p)
            value=finite(request['value']);previous=state['outputs'][axis]
            bounds=self.device['absolute_conditions']['axes'][axis]['activation_range']
            low=max(p.low,self.activation[axis]+bounds[0]);high=min(p.high,self.activation[axis]+bounds[1])
            if action=='locked_move' and axis=='temperature':
                from ldcon.owners.ecdl_tec_pair import slew_elapsed
                elapsed=slew_elapsed(axis,p,self.steady.p,self.clock()-self.last_write[axis])
            else:elapsed=min(self.clock()-self.last_write[axis],p.update_interval_s)
            if not low<=value<=high:raise DeviceError('Activation-relative or absolute bound exceeded')
            if self.clock()-self.last_write[axis]+1e-9<p.update_interval_s:raise DeviceError('Device update interval exceeded')
            if abs(value-previous)>min(p.max_step,p.max_slew_per_s*elapsed)+1e-9:raise DeviceError('Device step/slew limit exceeded')
            if axis!='piezo':
                half=state[axis]['amplitude_vpp']/2
                if value-half<low-1e-12 or value+half>high+1e-12:raise DeviceError('Candidate waveform exceeds analog envelope')
            if action=='locked_move':
                self.steady.sync_target_response()
                if self.steady._thermal_proposals_revoked:
                    raise MeasurementUnavailable('Thermal study ended before physical write; await a fresh proposal')
                self.steady.require_thermal_output(axis,value,state['outputs'])
            self.audit('move_requested',axis=axis,value=value,previous=previous,observation=optical)
            from ldcon.owners.sdg_client import SDGPrewriteUnavailable
            try:
                self.hw().move(axis,value);after=self.hw().snapshot()
                tolerance=self.device['control_settings']['axes'][axis]['readback_tolerance']
                if abs(after['outputs'][axis]-value)>tolerance:raise DeviceError('Applied output readback mismatch')
                self.validate_state(after,policies)
            except SDGPrewriteUnavailable:
                raise
            except Exception as error:
                self.fault='Uncertain output: '+str(error)
                raise
            self.last_write[axis]=self.clock()
            if recovery_move:self.last_recovery_frame=frame
            self.audit('move_applied',axis=axis,value=after['outputs'][axis])
            return after

    def close(self, hold=True):
        if not self.emission.shutdown():
            return False  # Keep transport/ownership while an uncertain write is still in flight.
        # hold=False (SIGTERM / service stop, 2026-10-22): no hardware write at shutdown -- the running firmware loop
        # does not depend on this process (rule: owners are thin and restartable).
        if hold and self.steady.armed:self.steady.hold('instrument owner shutdown')
        if not hold and self.steady.armed and not self.fault:
            # Design §5 (human decision 2026-10-26/27): a deliberate shutdown with a running native PI writes nothing
            # to the firmware, latches no fault and releases NO lease (neither the rig lease nor this process's
            # BK/SDG channel leases): the next owner process adopts the same lease after a read-only verification.
            self.steady.stop_event.set()
            deadline=self.steady.deadline
            record=dict(shutdown_unix_s=time.time(),lease=self.lease,reason='instrument owner shutdown',
                        heartbeat_remaining_s=None if deadline is None else deadline-self.clock(),
                        arm_reference=copy.deepcopy(self.steady.arm_reference))
            try:self.persist_state(shutdown=record)
            except (OSError,ValueError,TypeError) as error:
                record['persist_error']=str(error)   # the leases are still retained; the review sees the audit
            self.audit('owner_shutdown_active_lock',**record)
            hardware=self.hardware
            if hardware is not None and hasattr(hardware,'detach'):hardware.detach()
            return True
        if self.hardware:self.hardware.close()


def main():
    from ldcon.owners.instrument_service import make_server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--laser',type=Path,required=True)
    parser.add_argument('--audit',type=Path,required=True)
    parser.add_argument('--bind',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=47131)
    args=parser.parse_args()
    laser=load_mapping(args.laser.read_text())
    lock=open(f'/tmp/ldcon-ecdl-owner-{laser["id"]}.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    owner=ECDLOwner(laser,audit_path=args.audit)
    server=make_server(owner,args.bind,args.port,token=os.environ.get('LDCON_INSTRUMENT_TOKEN'))
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();owner.close();lock.close()


if __name__=='__main__':main()
