"""Read-only ldcon adapter for the shared RIGOL driver."""
from experiment_drivers.oscilloscopes.rigol_dho import RigolDHO


class RigolScope(RigolDHO):
    def __init__(self, host, port, device_id, serial=None):
        super().__init__(host, port, expected_model="DHO924", expected_serial=serial)
        self.device_id = device_id

    def read(self):
        return self.read_snapshot()
