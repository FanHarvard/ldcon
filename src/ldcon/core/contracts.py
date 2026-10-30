"""Device capabilities shared by cards and dummy drivers."""

from dataclasses import dataclass
from typing import Mapping, Protocol


class DeviceError(RuntimeError):
    """An expected device or measurement failure."""


class MeasurementUnavailable(DeviceError):
    """Owner explicitly rejected a command before I/O due to sensor quality."""


class OwnerUnavailable(MeasurementUnavailable):
    """The owner process did not answer a READ or liveness RPC (connection refused/reset, timeout): it is restarting
    or down. Nothing was written; the firmware PI keeps running inside its caps (rule B) and the owner adopts the
    same lease when it comes back (design §5, 2026-10-27). A lock card retries with the same lease for a bounded
    time (``owner_outage_timeout_s``) instead of failing. Shared by the native (sr1092) and ECDL (sr422) clients."""


@dataclass(frozen=True)
class FrequencySample:
    sequence: int
    frequency_hz: float | None
    acquired_s: float
    received_s: float
    quality: str
    source_id: str
    unit: str = "Hz"
    acquisition_known: bool = True
    acquired_unix_s: float | None = None


@dataclass(frozen=True)
class FringePattern:
    interferometer: str
    pixels: tuple[float, ...]
    exposure_ms: float | None = None
    quality: str = "valid"


@dataclass(frozen=True)
class FizeauFrame:
    frequency: FrequencySample
    channel: int
    patterns: tuple[FringePattern, ...]


class Wavemeter(Protocol):
    """Acquisition capability; vendor commands belong in a driver.

    Drivers convert vacuum wavelength to frequency, preserve quality, and
    identify acquisition time honestly. Pattern access and calibration control
    are optional capabilities, not requirements of a basic wavemeter.
    """

    def read_frequency(self) -> FrequencySample: ...


class Actuator(Protocol):
    unit: str
    minimum: float
    maximum: float
    max_slew_per_s: float

    def readback(self) -> float: ...

    def set_value(self, value: float, elapsed_s: float) -> None: ...


class FizeauWavemeter(Wavemeter, Protocol):
    """Frequency and one or more Fizeau interferograms from one acquisition."""

    def read_frame(self) -> FizeauFrame: ...


@dataclass(frozen=True)
class LaserRig:
    topology: str
    wavemeter: Wavemeter
    actuators: Mapping[str, Actuator]
    home: Mapping[str, float]
    primary: str
    primary_hz_per_unit: float
    target_hz: float
