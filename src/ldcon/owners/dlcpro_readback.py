"""Timestamped diagnostic readbacks; never block the servo on a full snapshot."""
from copy import deepcopy
import threading
import time


class HardwareReadback:
    def __init__(self, driver, channels, *, interval_s=2., max_age_s=5., clock=time.monotonic,
                 fault=lambda channel, reason: None):
        self.driver, self.channels, self.clock = driver, tuple(channels), clock
        self.interval_s, self.max_age_s = interval_s, max_age_s
        self.values = {}
        self.fault = fault
        self.stop = threading.Event()
        self.thread = None

    def refresh(self, channel):
        started = self.clock()
        try:
            with self.driver.console.lock.background():
                hardware = self.driver.snapshot(channel)
            self.values[channel] = (started, self.clock(), hardware, None)
            if hardware.get('tec_fault'):
                self.fault(channel, 'DLC TEC fault')
        except Exception as error:
            self.values[channel] = (started, self.clock(), None, type(error).__name__)

    def read(self, channel):
        started, ended, hardware, error = self.values.get(channel, (None, None, None, 'not_acquired'))
        age = None if started is None else self.clock()-started
        fresh = hardware is not None and 0 <= age <= self.max_age_s
        return (deepcopy(hardware) if fresh else {}, dict(started_s=started, completed_s=ended,
                age_s=age, fresh=fresh, error=error))

    def start(self):
        for channel in self.channels:
            self.refresh(channel)
        def run():
            while not self.stop.wait(self.interval_s):
                for channel in self.channels:
                    if self.stop.is_set():
                        return
                    self.refresh(channel)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
