"""Full-rate optical history contract, independent of storage/query syntax."""
from typing import Protocol


class WavelengthHistory(Protocol):
    def read(self, start_unix_s: float, end_unix_s: float) -> dict:
        """Return wavelength samples, source timestamps, sequence and provenance."""
        ...


def configured_reader(state_directory):
    from ldcon.drivers.influx_wavelength import InfluxWavelengthHistory
    from ldcon.jobs.shot_analysis import load_config
    config = load_config(state_directory)
    if config is None:
        raise ValueError('Configure shot-analysis.json before a target response study')
    return InfluxWavelengthHistory(config)
