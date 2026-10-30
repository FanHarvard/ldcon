"""Unlimited stationary lifetime, bounded individual attempts, separate PI capture."""
from unittest.mock import Mock, patch
import pytest
from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.recovery_stationary import StationaryRecovery
from .test_recovery_reference_v2 import config, history, settings
from ldcon.jobs.recovery_reference import infer


def controller():
    lock=Mock()
    steady=dict(target_frequency_hz=1000.,tolerance_hz=5.,strategy='hold',recovery=config())
    r=StationaryRecovery(lock,steady,clock=lambda:12.,sleep=Mock(),cancel=lambda:False,emit=Mock())
    r.reference=infer(history(),12.,'a',settings());r.reference['operation_epoch']='0'
    return r


def test_many_successes_do_not_disable_lifetime_recovery():
    r=controller()
    with patch('ldcon.jobs.recovery_stationary.BoundedExperiment'),patch('ldcon.jobs.recovery_stationary.execute',return_value=dict(final=dict(frequency_hz=1000.))):
        for _ in range(10):r.recover('test')
    assert r.recoveries==10 and r.lock.arm.call_count==10


def test_failed_or_uncertain_attempt_never_rearms():
    r=controller();r.armed=True
    with patch('ldcon.jobs.recovery_stationary.BoundedExperiment'),patch('ldcon.jobs.recovery_stationary.execute',side_effect=DeviceError('uncertain output')):
        with pytest.raises(DeviceError,match='uncertain'):r.recover('test')
    r.lock.hold.assert_called_once();r.lock.arm.assert_not_called()


def test_returned_frequency_does_not_relax_pi_capture():
    r=controller()
    with patch('ldcon.jobs.recovery_stationary.BoundedExperiment'),patch('ldcon.jobs.recovery_stationary.execute',return_value=dict(final=dict(frequency_hz=1100.))):
        with pytest.raises(DeviceError,match='capture'):r.recover('test')
    r.lock.arm.assert_not_called()


def test_new_target_cancels_before_rearm():
    r=controller()
    def completed(*args):
        r.steady['target_frequency_hz']=1001.
        return dict(final=dict(frequency_hz=1000.))
    with patch('ldcon.jobs.recovery_stationary.BoundedExperiment'),patch('ldcon.jobs.recovery_stationary.execute',side_effect=completed):
        with pytest.raises(CardCancelled):r.recover('test')
    r.lock.arm.assert_not_called()
