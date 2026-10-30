"""Scoped recovery history publication/loading; no commands, credentials or maps."""
import copy
import json
from pathlib import Path
import sqlite3
import time
from collections import deque

from ldcon.jobs.recovery_reference import finite, infer


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class Recorder:
    def __init__(self, laser_id, emit, recovery=None):
        self.laser_id, self.emit = laser_id, emit
        self.boot = boot_id()
        self.epoch = 0
        self.intentional = False
        self.target = None
        self.last = None
        self.settings = recovery
        self.samples = deque(maxlen=100000)
        self.reference = None
        self.event_open = False

    def invalidate(self):
        self.epoch += 1
        self.samples.clear()
        self.reference = None
        self.event_open = False

    def observe(self, row, target_hz, *, intentional=False):
        stamp = row.get('acquired_s')
        if self.target != target_hz:
            self.epoch += 1
            self.target = target_hz
            self.samples.clear()
            self.reference = None
            self.event_open = False
        if not finite(stamp) or self.last is not None and stamp <= self.last:
            return
        self.last = stamp
        active = bool(intentional or row.get('target_ramp_active'))
        if active and not self.intentional:
            self.epoch += 1
            self.samples.clear()
            self.reference = None
            self.event_open = False
        self.intentional = active
        sample = dict(acquired_s=stamp, frequency_hz=row.get('frequency_hz'),
            outputs=copy.deepcopy(row.get('outputs',{})), operation_epoch=str(self.epoch),
            intentional_operation=active,
            valid=finite(row.get('frequency_hz')) and row['frequency_hz'] > 0,
            target_frequency_hz=target_hz, laser_id=self.laser_id, boot_id=self.boot)
        self.emit('recovery_history_sample', sample)
        self.samples.append(sample)
        if not self.settings:return
        if sample['intentional_operation']:
            self.reference = None
            self.event_open = False
            return
        if self.reference and finite(sample['frequency_hz']):
            displaced = abs(sample['frequency_hz']-self.reference['frequency_hz']) > self.settings['hop_threshold_hz']
            if displaced:
                if not self.event_open:
                    self.emit('recovery_history_event', dict(event_time_s=stamp, operation_epoch=str(self.epoch),
                        reference=copy.deepcopy(self.reference), state='pending', laser_id=self.laser_id, boot_id=self.boot))
                    self.event_open = True
                return
            if self.event_open:
                self.emit('recovery_history_event', dict(event_time_s=stamp, operation_epoch=str(self.epoch),
                    reference=copy.deepcopy(self.reference), state='resolved', laser_id=self.laser_id, boot_id=self.boot))
            self.event_open = False
        try:self.reference = infer(self.samples, stamp, str(self.epoch), self.settings['reference'])
        except ValueError:pass


def load(directory, laser, p, *, clock=time.monotonic):
    """Newest same-laser controller job only; never fall back to another target/job.

    New workers publish source timestamps + operation epochs. Old unannotated
    telemetry cannot establish a trusted reference and needs fresh observations.
    """
    if p.get('history'):
        return p
    directory = Path(directory)
    root = directory.parent
    if root.name != 'jobs' or root.is_symlink():
        raise ValueError('Recovery history requires the canonical same-laser jobs directory')
    candidates = []
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink() or child == directory:
            continue
        meta_path = child/'job.json'
        if not meta_path.is_file() or meta_path.is_symlink() or meta_path.stat().st_size > 2**20:
            continue
        meta = json.loads(meta_path.read_text())
        if meta.get('laser',{}).get('id') != laser['id']:
            continue
        if not any(c.get('card',c.get('operation')) == 'servo.ecdl_steady_lock' for c in meta.get('cards',[])):
            continue
        candidates.append((meta.get('created_utc',''), child))
    if not candidates:
        raise ValueError('No same-laser steady history; collect fresh stable observations first')
    source = max(candidates)[1]
    path = source/'telemetry.sqlite3'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Latest controller has no readable recovery history')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1.) as db:
        raw = db.execute("SELECT payload_json FROM telemetry WHERE kind IN ('recovery_history_sample','recovery_history_event') ORDER BY id DESC LIMIT 100000").fetchall()
    records = [json.loads(row[0]) for row in reversed(raw)]
    samples = [r for r in records if 'acquired_s' in r]
    events = [r for r in records if 'event_time_s' in r]
    if not samples:
        raise ValueError('Latest controller predates recovery-history publication; collect a new stable window')
    latest = samples[-1]
    from ldcon.jobs.lock_target import resolve_target
    target = resolve_target(laser,{})
    if (latest.get('boot_id') != boot_id() or latest.get('laser_id') != laser['id']
            or latest.get('target_frequency_hz') != target):
        raise ValueError('Recovery history belongs to another boot, laser or target')
    now = clock()
    if not finite(latest.get('acquired_s')) or not 0 <= now-latest['acquired_s'] <= p['reference']['lookback_s']:
        raise ValueError('Latest recovery history expired')
    epoch = latest['operation_epoch']
    event_time = now
    if p.get('execution','recover') == 'recover':
        matching = [e for e in events if e.get('operation_epoch') == epoch and e.get('boot_id') == latest['boot_id'] and e.get('laser_id') == laser['id']]
        if not matching:
            # Reconstruct the same detector from trustworthy epoch-labelled raw
            # samples when steady recovery itself was disabled. The first
            # displaced plateau never becomes a new return target.
            detected = []
            detector = Recorder(laser['id'], lambda kind,row: detected.append(row) if kind=='recovery_history_event' else None, p)
            for sample in samples:
                if sample.get('operation_epoch') == epoch and sample.get('boot_id') == latest['boot_id']:
                    detector.observe(sample, target, intentional=sample.get('intentional_operation',False))
            matching = detected
        if not matching:
            raise ValueError('No recorded pre-hop event in current operation epoch; recovery must not infer a post-hop target')
        if matching[-1].get('state','pending')!='pending':
            raise ValueError('Latest recovery event is already resolved; do not replay an old route')
        event_time = matching[-1]['event_time_s']
        if not 0 <= now-event_time <= p['reference']['lookback_s']:
            raise ValueError('Recorded recovery event expired')
    history = [r for r in samples if r.get('boot_id') == latest['boot_id'] and r.get('laser_id') == laser['id']
               and r.get('operation_epoch') == epoch and finite(r.get('acquired_s'))
               and now-p['reference']['lookback_s'] <= r['acquired_s'] <= now]
    return {**p, 'history':history, 'event_time_s':event_time, 'operation_epoch':epoch}
