# Test-only archive. Never packaged, registered, or used by active recovery.
"""Use a tested, immutable map-replay recipe in the existing steady worker.

The tested procedure comes from ONE artifact (``recovery_artifact`` / ``recovery_ref``, ``ldcon.ecdl-recovery.v1``,
the ``prepare.ecdl_recovery`` card; human decision 2026-10-27). The deprecated plan/test pair
(``recovery_plan_*`` + ``recovery_test_*``) is still accepted for old artifacts. In ``restore`` mode the original
tested context is never translated following slow TEC drift: a recovery restores that context and verifies it again
before native PI rearm. In ``translate`` mode (default of the merged card) the TEC/PZT found at the hop are kept
when within the artifact's context tolerances and the verification uses the predicted frequency.

Re-arm handoff (hardware finding 2026-10-27): the procedure verifies the branch within the artifact's
``tolerance_hz`` (1 GHz) but the owner arms only within the acquisition tolerance (100 MHz capture band). After the
prime at lock start and after every successful in-lock recovery, the bounded open-loop trim of ``ecdl_rearm_trim``
(steady parameter ``rearm_trim``) brings the frequency into the acquisition tolerance before the arm; a branch loss
during the trim repeats the tested procedure (bounded by ``mapped_recovery_max_attempts``).
"""
import math
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, checked_artifact, finite
from .legacy_recovery_executor_fixture import checked_recovery, execute, experiment_parameters
from .legacy_recovery_plan_fixture import SCHEMA as PLAN_SCHEMA, piezo_hz_per_count_from_profile
from .legacy_recovery_test_fixture import (SCHEMA as TEST_SCHEMA, check_optical_context, context_mode,
                                           plan_translation, validate_context, verify_reference)
from .legacy_rearm_trim_fixture import TrimBranchLost, parameters as trim_parameters, trim_to_target
from ldcon.core.interface_cards import CardCancelled


class MappedStationaryRecovery:
    def __init__(self, lock, steady, *, clock, sleep, cancel, emit, laser=None):
        self.lock, self.steady = lock, steady
        self.clock, self.sleep, self.cancel, self.emit = clock, sleep, cancel, emit
        if steady.get('recovery_artifact'):
            # The merged artifact: plan and test evidence in ONE document (rule F: the card IS the procedure).
            self.plan = checked_recovery(steady['recovery_artifact'])
            self.proof = self.plan
        else:
            # Deprecated pair (plan.ecdl_recovery + test.ecdl_recovery artifacts of jobs before 2026-10-27).
            self.plan = checked_artifact(steady['recovery_plan_artifact'], PLAN_SCHEMA)
            self.proof = checked_artifact(steady['recovery_test_artifact'], TEST_SCHEMA)
            if (self.proof.get('plan_sha256') != self.plan['artifact_sha256']
                    or self.proof.get('map_sha256') != self.plan['map_sha256']
                    or self.proof.get('branch_id') != self.plan['branch_id']
                    or self.proof.get('verified') is not True
                    or not self.proof.get('trials')
                    or any(t.get('recovery_verified') is not True or t.get('hop_observed') is not True
                           for t in self.proof['trials'])):
                raise ValueError('Mapped recovery requires matching successful actual-hop test evidence')
        if abs(steady['target_frequency_hz'] - self.plan['selected_reference']['frequency_hz']) > self.plan['verification']['tolerance_hz']:
            raise ValueError('Steady target differs from the tested recovery context')
        self.max_recoveries = steady.get('mapped_recovery_max_attempts', 20)
        if type(self.max_recoveries) is not int or not 1 <= self.max_recoveries <= 100:
            raise ValueError('Invalid mapped recovery attempt budget')
        trim_parameters(steady.get('rearm_trim'))   # validated here; read again (hot value) at every trim
        self.ports = lock.recovery_ports()
        if not {'observation', 'loop'} <= self.ports.keys():
            raise ValueError('Mapped recovery requires typed optical and open-loop ports')
        self.p = experiment_parameters(self.plan, sample_interval_s=steady['sample_interval_s'],
                                       measurement_timeout_s=steady['measurement_timeout_s'])
        # PZT slope of the trim: the artifact's piezo_hz_per_count, else the laser profile's
        # steady_lock.wavelength_nm_per_count converted at the target (restore-mode artifacts carry none).
        self.trim_slope = self.plan.get('piezo_hz_per_count')
        if not finite(self.trim_slope) and laser is not None:
            try:
                self.trim_slope = piezo_hz_per_count_from_profile(laser, steady['target_frequency_hz'])
            except ValueError:
                self.trim_slope = None
        self.armed = False
        self.recoveries = 0
        self.hop_count = 0
        self.last_stamp = None
        self.trims = []          # every ecdl_rearm_trim record of this lock (start, then one per re-arm)
        self.trim_hops = 0       # branch losses during a trim (each one repeated the tested procedure)

    @property
    def context_mode(self):
        return context_mode(self.plan)

    def experiment(self):
        return BoundedExperiment(self.ports, dict(self.p), clock=self.clock, sleep=self.sleep,
                                 cancel=self.cancel, emit=self.emit)

    def acquisition_tolerance(self):
        return self.steady.get('acquisition_tolerance_hz') or self.steady['tolerance_hz']

    def trim(self, experiment, stage):
        """The bounded open-loop re-arm trim (ecdl_rearm_trim) toward the lock target; the record is kept for the
        job result. Raises TrimBranchLost when the branch does not follow a step."""
        try:
            record = trim_to_target(experiment, target_hz=self.steady['target_frequency_hz'],
                                    tolerance_hz=self.acquisition_tolerance(),
                                    hop_threshold_hz=self.plan['verification']['hop_threshold_hz'],
                                    piezo_hz_per_count=self.trim_slope, settings=self.steady.get('rearm_trim'),
                                    emit=self.emit, stage=stage)
        except TrimBranchLost as error:
            self.trims.append(error.record)
            raise
        self.trims.append(record)
        return record

    def trim_summary(self):
        return dict(count=len(self.trims), branch_losses=self.trim_hops,
                    counts_moved=sum(t['counts_moved'] for t in self.trims),
                    tec_moved=sum(t['tec_moved'] for t in self.trims),
                    start=next((t for t in self.trims if t['stage'] == 'lock start'), None),
                    last=self.trims[-1] if self.trims else None)

    def restore(self, stage):
        """THE tested procedure (execute) followed by the re-arm trim; a branch loss during the trim repeats the
        procedure. Bounded by mapped_recovery_max_attempts (each execution counts). Returns (verified row, trim)."""
        while True:
            self.recoveries += 1
            if self.recoveries > self.max_recoveries:
                raise DeviceError('Tested mapped recovery attempt budget exhausted')
            experiment = self.experiment()
            # The SAME tested procedure as the standalone recover card (rule F): hold -> execute -> trim -> re-arm.
            restored = execute(experiment, self.plan)
            try:
                trim = self.trim(experiment, stage)
            except TrimBranchLost as error:
                self.trim_hops += 1
                self.emit('ecdl_rearm_trim_hop', dict(stage=stage, reason=str(error), recoveries=self.recoveries,
                                                      max_recoveries=self.max_recoveries))
                continue
            return restored, trim

    def prime(self):
        experiment = self.experiment()
        validate_context(experiment, self.plan)
        initial = experiment.read('mapped steady initial context')
        check_optical_context(initial, self.plan)
        translation = plan_translation(experiment, self.plan) if self.context_mode == 'translate' else None
        verified = verify_reference(experiment, self.plan, stage='mapped steady initial qualification',
                                    translation=translation)
        self.last_stamp = verified['acquired_s']
        self.emit('ecdl_mapped_recovery_reference', dict(plan_sha256=self.plan['artifact_sha256'],
            test_sha256=self.proof['artifact_sha256'], recovery_sha256=self.proof['artifact_sha256'],
            context_mode=self.context_mode, reference=verified, reference_updates_enabled=False))
        # Lock start: trim toward the target before the card arms (the reference is verified within tolerance_hz,
        # the owner arms within the acquisition tolerance). A branch loss here is a hop before the lock existed:
        # the tested procedure brings the laser back (bounded), then the trim runs again.
        try:
            self.trim(experiment, 'lock start')
        except TrimBranchLost as error:
            self.trim_hops += 1
            self.emit('ecdl_rearm_trim_hop', dict(stage='lock start', reason=str(error), recoveries=self.recoveries,
                                                  max_recoveries=self.max_recoveries))
            restored, _ = self.restore('lock start')
            self.last_stamp = restored['acquired_s']

    def probe(self, *, update_reference=False):
        """Read independent optical evidence; never move a tested reference."""
        if self.cancel():
            raise CardCancelled('Mapped stationary recovery cancelled')
        try:
            row = self.lock.response_info()
        except (MeasurementUnavailable, OSError):
            self.hop_count = 0
            return False
        observation = row['observation']
        sample = observation['frequency']
        # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational; the source clock is used for age only when trusted+aligned.
        stamp = sample.get('acquired_unix_s')
        now = time.time()
        if stamp is None or sample.get('acquisition_known') is not True:
            stamp, now = sample.get('acquired_s'), self.clock()
        frequency = sample.get('frequency_hz')
        if (not finite(stamp) or not 0 <= now - stamp <= self.p['max_sample_age_s']
                or sample.get('unit') != 'Hz'
                or not finite(frequency) or frequency <= 0):
            self.hop_count = 0
            return False
        if self.last_stamp is not None:
            if stamp <= self.last_stamp:
                return False
            if stamp - self.last_stamp > max(3 * self.p['sample_interval_s'], 1.):
                self.hop_count = 0
        self.last_stamp = stamp
        lost = (abs(frequency - self.steady['target_frequency_hz']) > self.plan['verification']['hop_threshold_hz'])
        self.hop_count = self.hop_count + 1 if lost else 0
        if lost:
            self.emit('ecdl_mapped_recovery_candidate', dict(samples=self.hop_count,
                required_samples=self.plan['verification']['confirm_samples'],
                frequency_hz=frequency, mode_id=observation['mode_id'],
                plan_sha256=self.plan['artifact_sha256']))
        return self.hop_count >= self.plan['verification']['confirm_samples']

    def recover(self, reason):
        self.emit('state', dict(state='recovering', reason=reason,
                               plan_sha256=self.plan['artifact_sha256'], context_mode=self.context_mode))
        # Existing owner hold/unknown-write rules remain authoritative.
        if self.armed:
            held = self.lock.hold('tested map recovery: ' + reason)
            self.armed = False
            self.emit('ecdl_lock_held', held)
        restored, trim = self.restore('re-arm after recovery')
        if self.cancel():
            raise CardCancelled('Mapped recovery cancelled before native PI rearm')
        self.lock.check_ready(self.steady['strategy'])
        self.armed = True
        self.lock.arm(self.steady['target_frequency_hz'],
                      self.plan['verification']['tolerance_hz'], self.steady['strategy'])
        self.hop_count = 0
        self.last_stamp = restored['acquired_s']
        self.emit('ecdl_mapped_recovery_verified', dict(reference=restored, recovery=restored['recovery'],
            recoveries=self.recoveries, plan_sha256=self.plan['artifact_sha256'],
            test_sha256=self.proof['artifact_sha256'], rearm_trim=trim))
        self.emit('state', dict(state='locked', recovery_verified=True, recoveries=self.recoveries))
