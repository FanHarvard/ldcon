"""Bind semantic quantity IDs to storage; normalize units, quality and provenance."""
from dataclasses import asdict,dataclass
import math
from typing import Protocol
from ldcon.core.metric_quantities import QUANTITIES,conversion
from ldcon.drivers.metric_sources import DRIVERS,validate_source,validate_query
from ldcon.core.contracts import DeviceError


@dataclass(frozen=True)
class MetricSample:
    quantity:str
    unit:str
    value:float|None
    source_unix_s:float
    acquired_unix_s:float|None
    quality:str


class MetricReader(Protocol):
    def read(self,quantity:str,start:float,end:float)->dict:...
    def describe(self,quantity:str)->dict:...


def configuration(device):
    kind=device['type'];validate_source(kind,device['connection'])
    settings=device.get('control_settings',{})
    if set(settings)!={'bindings'} or not isinstance(settings['bindings'],dict) or not settings['bindings']:raise ValueError('Metric bindings are required')
    for quantity,b in settings['bindings'].items():
        if not isinstance(b,dict) or set(b)!={'source_unit','query','time_basis','description'}:raise ValueError('Invalid metric binding')
        conversion(quantity,b['source_unit']);validate_query(kind,b['query'])
        if b['time_basis'] not in ('acquisition','recording'):raise ValueError('Declare metric timestamp meaning')
        if not isinstance(b['description'],str) or not b['description']:raise ValueError('Describe source measurement/derivation')
    return settings['bindings']


class BoundMetrics:
    def __init__(self,device,driver=None):
        self.device=device;self.bindings=configuration(device)
        self.driver=driver or DRIVERS[device['type']](device['connection'])
    def describe(self,quantity):
        if quantity not in self.bindings:raise ValueError('Quantity has no source binding: '+quantity)
        return dict(quantity=asdict(QUANTITIES[quantity]),binding=self.bindings[quantity],
                    driver=self.device['type'],source=self.device['connection'])
    def read(self,quantity,start,end):
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or end<start or end-start>86400:raise ValueError('Invalid metric interval (maximum one day)')
        provenance=self.describe(quantity);b=self.bindings[quantity];scale,offset=conversion(quantity,b['source_unit'])
        try:raw=self.driver.read(b['query'],start,end)
        except (OSError,ValueError,KeyError,TypeError) as error:raise DeviceError('Metric source unavailable: '+str(error)) from error
        samples=[];seen={}
        for r in raw:
            t=r['time'];value=r['value'];q=r.get('quality')
            if type(t) not in (int,float) or not math.isfinite(t) or not start<=t<=end:raise DeviceError('Metric source returned invalid timestamps')
            if t in seen:
                if seen[t]!=r:raise DeviceError('Conflicting metric samples at the same timestamp')
                continue
            seen[t]=r
            valid=type(value) in (int,float) and math.isfinite(value)
            value=value*scale+offset if valid else None
            quality='invalid' if not valid or not math.isfinite(value) else 'valid' if q is True or (type(q) in (int,float) and q==1) else 'unknown' if q is None else 'invalid'
            if value is not None and not math.isfinite(value):value=None
            if quantity in ('optical.frequency','optical.wavelength','thermal.temperature') and value is not None and value<=0:quality='invalid'
            if b['time_basis']!='acquisition' and quality=='valid':quality='timestamp_unverified'
            samples.append(asdict(MetricSample(quantity,QUANTITIES[quantity].unit,value,t,t if b['time_basis']=='acquisition' else None,quality)))
        return dict(samples=samples,provenance=provenance)
    def close(self):pass


def router_configuration(device,devices):
    if device['connection'] or device.get('absolute_conditions'):raise ValueError('Metric router has no physical connection or safety output settings')
    settings=device.get('control_settings',{})
    if set(settings)!={'routes'} or not isinstance(settings['routes'],dict) or not settings['routes']:raise ValueError('Metric router requires quantity-to-source routes')
    for quantity,source in settings['routes'].items():
        if quantity not in QUANTITIES or not isinstance(source,str) or source not in devices:raise ValueError('Invalid metric source route')
        if devices[source]['type'] not in DRIVERS:raise ValueError('Route must reference a concrete metric source')
        if quantity not in configuration(devices[source]):raise ValueError('Routed source does not bind '+quantity)
    return settings['routes']


class RoutedMetrics:
    """One physical interface can retrieve different quantities from different stores."""
    def __init__(self,device,devices):
        self.routes=router_configuration(device,devices);self.devices=devices;self.ports={}
    def port(self,quantity):
        if quantity not in self.routes:raise ValueError('Quantity has no source route: '+quantity)
        source=self.routes[quantity]
        if source not in self.ports:self.ports[source]=BoundMetrics(self.devices[source])
        return self.ports[source]
    def describe(self,quantity):return {**self.port(quantity).describe(quantity),'source_device':self.routes[quantity]}
    def read(self,quantity,start,end):
        result=self.port(quantity).read(quantity,start,end)
        result['provenance']['source_device']=self.routes[quantity]
        return result
    def close(self):
        for port in self.ports.values():port.close()


def open_metric_interface(laser,role='metrics'):
    device=laser['devices'][laser['interfaces'][role]['device']]
    return RoutedMetrics(device,laser['devices']) if device['type']=='metrics.router' else BoundMetrics(device)
