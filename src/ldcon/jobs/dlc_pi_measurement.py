"""Shared change/measure adapter for an attached DLC software-PI epoch."""
from copy import deepcopy
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.jobs.dlcpro_cards import Session
from ldcon.owners.dlcpro_pi_trial import KEYS, candidate


class PIAdapter:
    def __init__(self, laser, identity, *, clock=time.monotonic, wall_clock=time.time):
        self.clock, self.wall_clock = clock, wall_clock
        self.laser_id = laser['id']
        self.device = laser['devices'][laser['interfaces']['dlc_lock']['device']]
        if not laser['allow_output']: raise ValueError('Laser output disabled')
        self.session = Session(self.device)
        self.token = None
        self.identity = identity
        self.state = self.session.rpc('pi_trial_state')
        self.origin = deepcopy(self.state['trial']['settings'])
        self.target = self.state['target_frequency_hz']
        self.origin_target = self.target
        self.target_program=None
        self.program_index=self.program_cycle=0
        self.acquisition_fence=float('-inf')
        self.target_revision = self.state['target_revision']
        self.samples, self.controls = [], []
        self.last_sample = self.state['frequency']['acquired_s']
        self.last_check = float('-inf')
        self.receipt = None
        self.uncertain = False
        self.measurement_missing = False
        self.family = 'pi'
        self.monitor_grant = None
        self.monitor_families = None
        self.operating_mode_revision = None
        self.operating_mode = self.state.get('operating_mode')
        self.begin_attempted = False
        self.begin_pending = False
        self.begin_confirmed = False

    def supervise(self, directory, p, laser):
        from ldcon.agents.dlc_monitor_grant import bind, check_grant
        from ldcon.agents.operating_mode_client import revision
        self.monitor_grant = bind(directory, p, laser)
        if self.monitor_grant is not None:
            self.monitor_families = check_grant(self.laser_id, self.monitor_grant)['bounds']['families']
        if self.operating_mode and self.operating_mode.get('mode') in ('measurement', 'characterization'):
            self.operating_mode_revision = revision(self.operating_mode)
        elif self.monitor_grant is not None:
            raise ValueError('Monitored adjustment requires current owner operating mode')

    def begin(self):
        supervision = {}
        if self.operating_mode_revision is not None:
            supervision['expected_operating_mode_revision'] = self.operating_mode_revision
        if self.monitor_grant is not None:supervision['monitor_grant'] = self.monitor_grant
        if self.family != 'pi':supervision['family'] = self.family
        self.begin_attempted = self.begin_pending = True
        try:
            receipt = self.session.rpc('pi_trial_begin', identity=self.identity,
                device_revision=digest(self.device), target_revision=self.target_revision,
                policy_revision=self.state['policy_revision'], watchdog_s=30.,target_program=self.target_program, **supervision)
        except BrokerError as error:
            if error.code in ('measurement','configuration','ownership','range'):self.begin_pending = False
            raise
        self.token = receipt['token']
        self.begin_pending = False
        self.begin_confirmed = True
        self.origin = deepcopy(receipt['settings'])
        self.check(force=True)

    def check(self, force=False):
        if not force and self.clock()-self.last_check < .2: return
        if self.monitor_grant is not None:
            from ldcon.agents.dlc_monitor_grant import check_grant
            check_grant(self.laser_id, self.monitor_grant)
        if self.token:
            self.session.rpc('pi_trial_heartbeat', token=self.token)
            history = self.session.rpc('pi_trial_samples', token=self.token, after_s=self.last_sample)
            for sample in history['samples']:
                if sample['acquired_s'] > self.last_sample:
                    self.samples.append(sample);self.last_sample=sample['acquired_s']
        try:
            self.state = self.session.rpc('pi_trial_state')
            self.measurement_missing = False
        except BrokerError as error:
            if error.code != 'measurement':raise
            # Fresh control/context evidence remains available without optics.
            # Keep the old acquisition timestamp; read() reports missing data.
            self.state.update(self.session.rpc('pi_trial_receipt'))
            self.measurement_missing = True
        self.last_check = self.clock()
        self.controls.append(deepcopy(self.state))
        if self.operating_mode_revision is not None:
            mode = self.state.get('operating_mode')
            if not mode or mode.get('revision') != self.operating_mode_revision:
                raise CardCancelled('Operating mode changed; adjustment ends')
        if self.state['mode'] != 'lock' or self.state['fault'] or self.state['target_revision'] != self.target_revision:
            raise DeviceError('Lock/target context changed; adjustment ends')
        if not self.measurement_missing and self.state['frequency']['acquired_s']>self.acquisition_fence and abs(self.state['frequency']['frequency_hz']-self.target) > self.state['control']['capture_hz']:
            raise DeviceError('PI trial left capture neighborhood; restore parameters without changing target')

    def preflight(self, changes, mode, measurements, planned=None):
        self.check(force=True)
        if self.measurement_missing:
            raise MeasurementUnavailable('Await new source acquisition before a PI/target proposal')
        if mode != 'atomic' or measurements.get('dither') or measurements.get('optical_quality'):
            raise ValueError('Attached DLC PI uses atomic settings and source wavelength measurements')
        if changes and changes[0]['parameter']=='target_frequency_hz':
            if len(changes)!=1 or self.target_program is None:raise ValueError('Target move needs frozen response program')
            expected=self.origin_target+self.target_program['offsets_hz'][self.program_index]
            value=changes[0].get('value',self.target+changes[0].get('offset',0.))
            if value!=expected:raise ValueError('Target differs from the next frozen program step')
            return dict(target=value,revision=self.target_revision,index=self.program_index,cycle=self.program_cycle)
        old = self.state['trial']['settings']
        values = deepcopy(old)
        for change in changes:
            if change['parameter'] not in KEYS: raise ValueError('Only PI coefficients and integral contribution are admitted')
            key=change['parameter'];values[key]=change.get('value',old[key]+change.get('offset',0.))
        candidate(values)
        return dict(candidate=values, checkpoint=old, revision=self.state['trial']['revision'],
                    mode=mode, binding='dlc_pi', planned=values)

    def apply(self, plan, sample):
        self.uncertain = True
        before=self.wall_clock()
        def command(action,**kwargs):
            try:return self.session.rpc(action,**kwargs)
            except BrokerError as error:
                if error.code in ('measurement','configuration','ownership','range'):
                    self.uncertain=False
                if error.code=='measurement':raise MeasurementUnavailable(str(error)) from error
                raise
        if 'target' in plan:
            receipt=command('pi_trial_target_step',token=self.token,
                expected_revision=plan['revision'],index=plan['index'],cycle=plan['cycle'])
            self.target=receipt['target_frequency_hz'];self.target_revision=receipt['target_revision']
            self.acquisition_fence=receipt['acquisition_fence_s']
            self.program_index+=1
            if self.program_index==len(self.target_program['offsets_hz']):self.program_index=0;self.program_cycle+=1
            receipt['readback']={'target_frequency_hz':self.target}
        else:
            receipt=command('pi_trial_apply',token=self.token,
                expected_revision=plan['revision'],candidate=plan['candidate'])
        self.uncertain = False
        self.receipt = receipt
        return dict(status='applied',readback=receipt['readback'],owner_result=receipt,
                    started_unix_s=before,completed_unix_s=self.wall_clock())

    def read(self, measurements):
        self.check()
        if self.measurement_missing:raise MeasurementUnavailable('Source acquisition temporarily unavailable')
        f=self.state['frequency']
        return {'wavelength':dict(valid=True,acquired_unix_s=f['acquired_unix_s'],
            values=dict(frequency_hz=f['frequency_hz'],wavelength_nm=299792458e9/f['frequency_hz']))}

    def complete_measurement(self, result, **kw):
        self.check(force=True)
        # Replace coalesced polling samples by the complete source acquisitions.
        # Preserve phase boundaries and their real timestamps. No interpolation.
        begin=result['baseline_started_unix_s'];end=result['measurement_ended_unix_s']
        apply=result['application_completed_unix_s'];measure=result['measurement_started_unix_s']
        result['samples'][:]=[dict(phase='measurement' if f['acquired_unix_s']>=measure else 'wait' if f['acquired_unix_s']>=apply else 'baseline',
            received_unix_s=None,measurements={'wavelength':dict(valid=True,acquired_unix_s=f['acquired_unix_s'],
                values=dict(frequency_hz=f['frequency_hz'],wavelength_nm=299792458e9/f['frequency_hz']))})
            for f in self.samples if begin<=f['acquired_unix_s']<=end]
        result['source_acquisitions']=[deepcopy(f) for f in self.samples if begin<=f['acquired_unix_s']<=end]
        result['control_observations']=[x for x in self.controls if begin<=x['frequency']['acquired_unix_s']<=end]
        result['raw_source']='owner WSU1 acquisition history; control observations have their own timestamps'

    def close(self, adopt=False):
        if self.token is None:
            if self.begin_pending:raise DeviceError('PI begin outcome is unconfirmed; do not replay')
            return None
        cached=self.session.rpc('pi_trial_receipt');state=cached['trial']
        if self.uncertain or cached['fault']:
            raise DeviceError('Unknown physical effect or owner fault; coefficient receipt is not output verification')
        if state['active'] is None:
            last=state['last']
            if not last or last['identity']!=self.identity:raise DeviceError('PI cleanup receipt unavailable')
            self.token=None
            return last
        try:
            # Only the explicitly admitted target-response program owns target
            # excursions. Current/TEC/FF and ordinary PI trials never restore it.
            if self.family=='pi' and self.target_program is not None and cached['target_frequency_hz']!=self.origin_target:
                self.uncertain=True
                self.session.rpc('pi_trial_restore_target',token=self.token)
                self.uncertain=False
            self.uncertain=True
            result=self.session.rpc('pi_trial_finish',token=self.token,adopt=adopt)
            if result.get('confirmed') is not True:raise DeviceError('Trial finish is unconfirmed')
            self.uncertain=False
        except BrokerError as error:
            if error.code not in ('configuration','ownership'):raise
            self.uncertain=False  # Explicit pre-write refusal; reconcile only.
            # Revocation can restore between the cached read and finish. This
            # is a read-only reconciliation, never a second finish or adoption.
            latest=self.session.rpc('pi_trial_receipt')
            last=latest.get('trial',{}).get('last')
            if (self.uncertain or latest.get('fault') or latest.get('trial',{}).get('active') is not None
                    or not last or last.get('identity')!=self.identity or last.get('confirmed') is not True):
                raise
            result=last
        self.token=None
        return result

    def physical_outcome_on_error(self, error):
        return 'unknown' if self.uncertain else 'not_applied'
