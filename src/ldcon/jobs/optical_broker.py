"""Frequency read proxy; only the instrument owner contacts the wavemeter."""
import json
import os
from urllib.request import Request,build_opener,ProxyHandler
from ldcon.core.contracts import FrequencySample,DeviceError
from ldcon.core.interface_schema import digest


class BrokerFrequencyPort:
    def __init__(self,device):
        self.device=device;self.http=build_opener(ProxyHandler({}))

    def read_frequency(self):
        c=self.device['connection']
        request=Request(c['endpoint']+'/rpc',data=json.dumps(dict(action='sensor_read',
            instrument=c['instrument'],revision=digest(self.device))).encode(),headers={'Content-Type':'application/json'})
        token=os.environ.get('LDCON_INSTRUMENT_TOKEN')
        if token:request.add_header('Authorization','Bearer '+token)
        try:
            with self.http.open(request,timeout=10) as response:result=json.load(response)
            if result.get('ok') is not True:raise DeviceError(result.get('error','Optical owner error'))
            return FrequencySample(**result['result'])
        except (OSError,ValueError,KeyError,TypeError) as error:raise DeviceError(str(error)) from error
