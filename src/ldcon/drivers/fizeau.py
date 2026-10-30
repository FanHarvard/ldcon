"""Read-only driver for the WS-U HTTP pattern API used by ECDLcontrol."""

import json
import math
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from ldcon.core.contracts import DeviceError, FizeauFrame, FrequencySample, FringePattern


class HttpFizeauWavemeter:
    """Preserves simultaneous multiple patterns; never infers wavelength from pixels.

    Source clock alignment must be explicitly trusted before acquisition age
    can be used for actuation. Otherwise frames remain available to monitors
    with acquisition_known=False. No instrument configuration calls are made.
    """

    def __init__(self, base_url, channel=1, timeout_s=3, trust_source_clock=False,
                 fetch_json=None, clock=time.monotonic, utc_clock=time.time, cycle=None, expected_laser=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("A plain HTTP(S) base URL is required")
        if type(channel) is not int or not 1 <= channel <= 64:
            raise ValueError("Invalid wavemeter channel")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("Timeout must be within (0, 30] seconds")
        if cycle is not None and (type(cycle) is not int or cycle<0):raise ValueError('Invalid wavemeter cycle')
        if expected_laser is not None and (not isinstance(expected_laser,str) or not expected_laser):raise ValueError('Invalid expected laser label')
        self.expected_laser=expected_laser
        self.cycle=cycle
        query={"channel":channel,"interferometer":"both"}
        if cycle is not None:query['cycle']=cycle
        self.url=base_url.rstrip('/')+'/api/pattern?'+urlencode(query)
        self.channel, self.timeout_s = channel, timeout_s
        self.trust_source_clock = trust_source_clock
        self.fetch_json = fetch_json or self._fetch
        self.clock, self.utc_clock = clock, utc_clock
        self.sequence = 0

    def _fetch(self, url):
        with urlopen(url, timeout=self.timeout_s) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise DeviceError("Fizeau frame exceeds maximum response size")
        return json.loads(data)

    def read_frame(self, *, allow_missing_frequency=False):
        try:
            data = self.fetch_json(self.url)
            self.last_source_metadata={k:v for k,v in data.items() if k!='patterns'}
            self.last_source_metadata['patterns']=[{k:v for k,v in item.items() if k!='data'} for item in data.get('patterns',[])]
            received = self.clock()
            if data.get("ok") is not True or data.get("channel") != self.channel:
                raise ValueError("Unsuccessful response or channel mismatch")
            if self.expected_laser is not None and data.get('record',{}).get('laser')!=self.expected_laser:raise ValueError('Wavemeter laser label mismatch')
            if self.cycle is not None:
                observed=data.get('cycle',data.get('record',{}).get('cycle'))
                if type(observed) is not int or observed!=self.cycle:raise ValueError('Unverified or mismatched wavemeter cycle')
            missing = (('frequency_thz' in data or 'wavelength_nm' in data) and
                       data.get('frequency_thz') is None and data.get('wavelength_nm') is None)
            hz = (None if missing and allow_missing_frequency else
                  float(data["frequency_thz"]) * 1e12 if data.get('frequency_thz') is not None
                  else 299792458.0 / (float(data["wavelength_nm"]) * 1e-9))
            if hz is not None and (not math.isfinite(hz) or hz <= 0):
                raise ValueError("Invalid optical frequency")
            raw_patterns = data["patterns"]
            if not 1 <= len(raw_patterns) <= 16:
                raise ValueError("Expected 1..16 interferograms")
            patterns = []
            for item in raw_patterns:
                pixels = tuple(float(x) for x in item["data"])
                if (not 4 <= len(pixels) <= 65536 or not all(math.isfinite(x) for x in pixels)
                        or item.get("count", len(pixels)) != len(pixels)):
                    raise ValueError("Invalid interferogram pixels/count")
                exposure = item.get("exposure_ms")
                if exposure is not None and (not math.isfinite(float(exposure)) or float(exposure) < 0):
                    raise ValueError("Invalid exposure")
                patterns.append(FringePattern(
                    str(item["interferometer"]), pixels,
                    float(exposure) if exposure is not None else None,
                    "valid" if item.get("consistent", True) is True else "inconsistent",
                ))
            if len({pattern.interferometer for pattern in patterns}) != len(patterns):
                raise ValueError("Duplicate interferometer identifiers")
            quality = "valid" if data.get("status", 0) == 0 and data.get("quality", 0) == 0 else "invalid"
            acquired_unix = data.get("t_exposure_end_unix_ms")
            acquired_unix = float(acquired_unix) / 1000 if acquired_unix is not None else None
            if acquired_unix is not None and not math.isfinite(acquired_unix):
                raise ValueError("Invalid acquisition timestamp")
            known = (self.trust_source_clock and acquired_unix is not None
                     and data.get("record", {}).get("aligned") is True)
            acquired = received - (self.utc_clock() - acquired_unix) if known else received
            self.sequence += 1
            sample = FrequencySample(self.sequence, hz, acquired, received, quality if hz is not None else 'invalid',
                                     f"fizeau.channel-{self.channel}", acquisition_known=known,
                                     acquired_unix_s=acquired_unix)
            return FizeauFrame(sample, self.channel, tuple(patterns))
        except (OSError, URLError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            raise DeviceError(f"Fizeau frame unavailable or malformed: {error}") from error

    def read_frequency(self):
        return self.read_frame().frequency


def pattern_metrics(pattern):
    """Basic visibility metrics; not a single-mode certification."""
    low, high = min(pattern.pixels), max(pattern.pixels)
    denominator = high + low
    return {"minimum": low, "maximum": high,
            "mean": sum(pattern.pixels) / len(pattern.pixels),
            "visibility": (high - low) / denominator if denominator > 0 else 0.0}
