"""Independent UDP control-frequency and cached HTTP optical-quality paths."""
import math
import threading
import time

from ldcon.core.contracts import MeasurementUnavailable


class DatagramOpticalSource:
    def __init__(self,receiver,optical,*,quality_interval_s,quality_max_age_s,clock=time.monotonic):
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (quality_interval_s,quality_max_age_s)) or not 0<quality_interval_s<=quality_max_age_s:
            raise ValueError('Positive quality cadence and freshness required')
        self.receiver=receiver;self.optical=optical;self.clock=clock
        self.interval=quality_interval_s;self.max_age=quality_max_age_s
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None
        self.cached=None;self.cached_at=None;self.error='not acquired'

    def refresh_quality(self):
        try:
            value=self.optical.read_mode()
            with self.lock:self.cached=value;self.cached_at=self.clock();self.error=None
        except Exception as error:
            # Read-only auxiliary failure; never a hardware-write exception.
            with self.lock:self.error=type(error).__name__

    def quality_snapshot(self):
        with self.lock:
            frame=self.cached
            age=None if frame is None else self.clock()-frame.frequency.acquired_s
            fresh=age is not None and 0<=age<=self.max_age and frame.frequency.acquisition_known
            return dict(available=frame is not None,fresh=fresh,age_s=age,error=self.error,
                acquired_s=None if frame is None else frame.frequency.acquired_s,
                quality_valid=bool(fresh and frame.quality_valid),
                single_mode=None if not fresh else frame.single_mode,
                metrics={} if not fresh else dict(frame.metrics),
                mode_id=None if not fresh else frame.mode_id)

    def read_frequency(self):return self.receiver.read_frequency()
    def samples_after(self,stamp):return self.receiver.samples_after(stamp)
    def input_snapshot(self):return self.receiver.snapshot()

    def read_mode(self):
        with self.lock:
            quality=self.quality_snapshot()
            if not quality['fresh'] or not quality['quality_valid']:
                raise MeasurementUnavailable('Optical pattern cache unavailable; UDP frequency remains independent')
            # Preserve the original coherent frame and its acquisition identity.
            return self.cached

    def start(self):
        if self.thread is not None:raise RuntimeError('Optical cache already started')
        self.receiver.start()
        def run():
            while not self.stop.is_set():
                self.refresh_quality()
                if self.stop.wait(self.interval):break
        self.thread=threading.Thread(target=run,name='dlc-optical-cache',daemon=True);self.thread.start()
        return self

    def close(self):
        self.stop.set();self.receiver.close()
        if self.thread is not None:self.thread.join(timeout=1)
