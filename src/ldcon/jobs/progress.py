"""Bounded progress snapshots for interface workers."""

import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback

from ldcon.core.records import atomic_json, read_json, utc_now


class CancelledRun(BaseException):
    pass


def finite_values(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,dict):return {k:finite_values(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [finite_values(v) for v in value]
    return value


class ProgressSink:
    def __init__(self, directory):
        self.directory=directory;self.sent=0;self.dropped=0;self.next_write=0
        self.cancelling=False
        self.state={'status':'running','simulated_s':0,'active_card':None,
                    'controller_state':'acquiring','mode_transitions':0}
        self.readings={};self.points=[]

    def send(self,run_id,simulated_s,kind,payload):
        if not self.cancelling and (self.directory/'cancel.request').exists():
            self.cancelling=True
            raise CancelledRun()
        self.sent+=1;self.state['simulated_s']=max(simulated_s,self.state['simulated_s'])
        if kind=='state':self.state['controller_state']=payload.get('state')
        if kind=='mode_transition':self.state['mode_transitions']+=1
        if kind in ('card_start','recovery_start'):self.state['active_card']=payload.get('card')
        if kind=='card_end':self.state['active_card']=None
        if kind=='trace':self.readings={'time_s':simulated_s,**payload}
        now=time.monotonic()
        if now>=self.next_write or kind in ('state','card_start','card_end','recovery_start','finished'):
            if self.readings and (not self.points or self.points[-1].get('time_s')!=self.readings.get('time_s')):
                self.points.append(finite_values(self.readings))
                self.points=self.points[-600:]
            self.state['updated_utc']=utc_now()
            atomic_json(self.directory/'progress.json',finite_values(self.state))
            atomic_json(self.directory/'readings.json',finite_values(self.readings))
            atomic_json(self.directory/'live-trace.json',self.points)
            self.next_write=now+.35

    def observe(self,simulated_s,row):
        self.readings=finite_values(row)
        if not self.points or self.points[-1].get('time_s')!=simulated_s:
            self.points.append(self.readings);self.points=self.points[-600:]
        self.send(None,simulated_s,'progress',{})

    def close(self):
        pass

