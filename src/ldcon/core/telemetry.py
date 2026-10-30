"""Durable telemetry and run history; no external telemetry server required."""

import json
import math
import os
from pathlib import Path
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4


class TelemetryStore:
    """One synchronous writer, committing each record in this prototype.

    A persistence error propagates before the next control action. Production
    high-rate capture will require bounded queues, batching, retention and an
    explicit overflow policy. SQLite access is confined to its owning thread.
    """

    def __init__(self, path, exporter=None, *, modulation_root=None):
        self.exporter = exporter
        self.modulation_root = modulation_root
        self.run_ids = set()
        self.last_times = {}
        self.contexts={}
        self.card_operations={}
        self.event_log=None
        if str(path)!=':memory:':
            # A shared append-only human/tool-readable mirror; SQLite remains
            # authoritative and retains complete structured payloads.
            self.event_log=open(Path(path).with_suffix('.events.jsonl'),'a',encoding='utf-8')
        self.connection = sqlite3.connect(path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started_utc TEXT NOT NULL,
                status TEXT NOT NULL, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                recorded_utc TEXT NOT NULL, simulated_s REAL NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS telemetry_run ON telemetry(run_id, id);
        """)
        self.connection.execute("PRAGMA foreign_keys = ON")

    def start_run(self, config):
        run_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)",
                (run_id, self.utc_now(), "running", json.dumps(config, allow_nan=False)),
            )
        self.run_ids.add(run_id)
        laser=config.get('laser',{})
        laser=laser if isinstance(laser,dict) else {}
        invocation=config.get('invocation',{});deck=config.get('deck',{})
        deck_id=deck.get('id') if isinstance(deck,dict) else deck if isinstance(deck,str) else None
        deck=deck if isinstance(deck,dict) else {}
        cards=deck.get('cards',config.get('cards',[]))
        self.card_operations[run_id]={c['id']:c.get('operation',c.get('catalog',c.get('card')))
            for c in cards if isinstance(c,dict) and 'id' in c}
        card_id=invocation.get('card_id')
        card=next((c for c in deck.get('cards',[]) if isinstance(c,dict) and c.get('id')==card_id),{})
        self.contexts[run_id]={k:v for k,v in dict(laser_id=laser.get('id'),
            card=card_id,operation=card.get('operation'),deck=deck_id).items() if v is not None}
        return run_id

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc).isoformat()

    def record(self, run_id, simulated_s, kind, payload):
        payload=dict(payload)
        context=dict(self.contexts.get(run_id,{}))
        if isinstance(payload.get('card'),str):context['card']=payload['card']
        if kind=='card_start' and isinstance(payload.get('card'),str):
            operation=payload.get('operation',payload.get('catalog_id',self.card_operations.get(run_id,{}).get(payload['card'])))
            if operation is not None:context['operation']=operation
            self.contexts[run_id]=dict(context)
        context.update(payload.get('_context',{}))
        if context:payload['_context']=context
        from ldcon.core.modulation import enrich,DEFAULT_ROOT
        payload=enrich(payload,context.get('laser_id'),self.modulation_root or DEFAULT_ROOT)
        recorded=self.utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO telemetry VALUES (NULL, ?, ?, ?, ?, ?)",
                (run_id, recorded, simulated_s, kind,
                 json.dumps(payload, allow_nan=False)),
            )
        self.last_times[run_id] = simulated_s
        if self.event_log is not None:
            self.event_log.write(json.dumps(dict(run_id=run_id,recorded_utc=recorded,
                elapsed_s=simulated_s,kind=kind,payload=payload),allow_nan=False,ensure_ascii=False)+'\n')
            self.event_log.flush();os.fsync(self.event_log.fileno())
        if self.exporter is not None:
            self.exporter.send(run_id, simulated_s, kind, payload)

    def sample(self, run_id, simulated_s, sample):
        payload = asdict(sample)
        for key, value in list(payload.items()):
            if isinstance(value, float) and not math.isfinite(value):
                payload[key] = None
                payload[f"raw_{key}"] = repr(value)
        self.record(run_id, simulated_s, "sample", payload)

    def finish(self, run_id, status):
        with self.connection:
            self.connection.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def close(self):
        try:
            if self.exporter is not None:
                for run_id in self.run_ids:
                    self.record(run_id, self.last_times.get(run_id, 0.0), "export_status", {
                        "sent": self.exporter.sent, "dropped": self.exporter.dropped,
                    })
        finally:
            if self.exporter is not None:
                self.exporter.close()
            if self.event_log is not None:self.event_log.close()
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
