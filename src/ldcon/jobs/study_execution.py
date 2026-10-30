"""Shared study decision port: bounded resident analysis of recorded evidence.

The common core validates proposals. This adapter has no owner/control port and
never installs parameters. Domain studies supply evidence, actions and evaluators.
"""
import json
import time
from pathlib import Path

from experiment_core.execution import Context, decide
from experiment_core.ownership import Grant
from experiment_core.records import Journal, digest

from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json, read_json

DEFAULTS = dict(execution_mode='deterministic', agent_card_id='gateway-agent', agent_timeout_s=300., execution_profile='', adoption_mode='report_only')


def validate(p):
    from ldcon.core.interface_schema import identifier
    if p['execution_mode'] not in ('deterministic', 'agent'):
        raise ValueError('Execution mode must be deterministic or agent')
    identifier(p['agent_card_id'])
    if p['execution_profile']:identifier(p['execution_profile'])
    from experiment_core.agent_roles import adoption_mode
    adoption_mode(p['adoption_mode'])
    if type(p['agent_timeout_s']) not in (float, int) or not 10 <= p['agent_timeout_s'] <= 900:
        raise ValueError('Agent analysis deadline must be 10..900 seconds')


def resident(directory, card_id):
    """Admission before measurement; disconnected residents do not start a study."""
    from ldcon.agents.resident_agent import context
    store, meta = context(directory)
    key = store.key(meta, card_id)
    session = store.session(key)
    if not session or session['status'] not in ('ready', 'busy'):
        raise ValueError('Selected resident is unavailable; start it or choose measurement only')
    parent = Path(directory).parent / session['job']
    job = read_json(parent/'job.json', {})
    # Job status is persisted in progress on some versions, job.json on others.
    progress = read_json(parent/'progress.json', {})
    if (progress.get('status', job.get('status')) not in ('running',)
            or job.get('set_id') != meta['set_id']):
        raise ValueError('Selected resident job is not running for this laser')
    return store, meta, key, session


def decision_parent(directory, card_id):
    """A result-routing parent, not a liveness dependency for managed card work."""
    from ldcon.agents.resident_agent import context
    store,meta=context(directory);key=store.key(meta,card_id)
    session=store.session(key) or dict(job=None,session=None,provider=None,status='absent')
    return store,meta,key,session


def export_evidence(directory, store, laser_id, documents):
    """Bounded chunks usable by existing laser_files_read; originals stay in job."""
    from ldcon.core.laser_assets import LaserAssets
    assets = LaserAssets(store.root.parent, laser_id)
    manifest, total = [], 0
    # Each document must already be a meaningful, independently readable chunk.
    encoded = [(name, json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
               for name, value in documents]
    if sum(len(value) for _, value in encoded) > 4*1024*1024:
        raise ValueError('Study evidence exceeds the resident analysis budget; original data retained')
    for name, content in encoded:
        path = 'data/study-'+Path(directory).name+'/'+name+'.json'
        receipt = assets.write(path, content, '')
        total += len(content)
        manifest.append(dict(path=path, sha256=receipt['sha256'], bytes=len(content)))
    atomic_json(Path(directory)/'study-evidence.json', dict(files=manifest, bytes=total))
    return assets, manifest


def parse_proposal(text, *, allow_progress_prefix=False):
    text = text.strip()
    if not text.startswith(('{', '[')) and '```' in text:
        # ACP may concatenate progress prose with the final fenced decision.
        # Accept one explicitly delimited payload, never choose among proposals
        # or infer an object from arbitrary prose/truncated output.
        parts = text.split('```')
        if len(parts) != 3 or any(c in parts[0] + parts[2] for c in '{}[]'):
            raise ValueError('Expected exactly one unambiguous JSON decision fence')
        language, separator, payload = parts[1].partition('\n')
        if not separator or language.strip() not in ('', 'json'):
            raise ValueError('Expected a complete JSON decision fence')
        text = payload.strip()
    if allow_progress_prefix and not text.startswith(('{', '[')):
        # ACP can join progress text directly to its final structured decision.
        # Exactly one complete trailing object is eligible. Any structured prefix,
        # ambiguous second object, trailing prose or truncation is still rejected.
        index=text.find('{')
        prefix=text[:index] if index>=0 else text
        if index<0 or any(c in prefix for c in '{}[]'):
            raise ValueError('Expected one final decision object after plain progress text')
        text=text[index:]
    # Do not extract arbitrary JSON from prose or guess a truncated answer.
    return json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite proposal')))


class ResidentDecisions:
    def __init__(self, directory, selected, *, clock, sleep, cancel, publish):
        self.directory, self.selected = Path(directory), selected
        self.clock, self.sleep, self.cancel, self.publish = clock, sleep, cancel, publish
        self.request_id = 'study-decision:'+self.directory.name

    def propose(self, evidence, actions, budget):
        store, meta, key, session = self.selected
        payload = dict(kind='study_evaluation', job=meta['id'], evidence=evidence,
                       allowed_actions=actions,
                       prompt=evidence['instructions'] + '\nReturn only JSON with exactly action and parameters. '
                       'Read all listed evidence chunks with laser_files_read before deciding. '
                       'Do not send notifications, modify configuration, start/stop cards, change targets, '
                       'or perform any physical actions. This request grants analysis only.')
        store.enqueue(key, self.request_id, payload)
        self.publish(dict(state='waiting_for_agent', request_id=self.request_id,
                          agent_card_id=json.loads(key)[2]))
        try:
            while True:
                if self.cancel():
                    raise CardCancelled('Study analysis cancelled; measured data and plots retained')
                if self.clock() >= budget['deadline']:
                    raise TimeoutError('Study agent analysis deadline expired')
                row = store.request(key, self.request_id)
                if row['status'] == 'completed':
                    atomic_json(self.directory/'study-agent-answer.json', dict(request_id=self.request_id,
                        provider=session['provider'], session=session['session'], answer=row['result']))
                    return parse_proposal(row['result'])
                if row['status'] not in ('queued', 'running'):
                    raise ValueError('Study agent response '+row['status']+'; no automatic replay')
                self.sleep(min(.2, max(0., budget['deadline']-self.clock())))
        finally:
            # A claimed read-only turn may finish later; no late result is adopted.
            with store.connect() as db:
                db.execute("UPDATE requests SET status='cancelled',result='Study decision wait ended before claim',updated=? "
                           "WHERE key=? AND id=? AND status='queued'", (time.time(), key, self.request_id))


def evaluate(directory, selected, *, laser_id, documents, objective, actions,
             timeout_s, clock, sleep, cancel, publish, provider=None):
    """Reusable meaningful decision boundary; no device context is passed."""
    from ldcon.agents.agent_bundle import build
    directory = Path(directory)
    store, meta, _, _ = selected
    assets, manifest = export_evidence(directory, store, laser_id, documents)
    bundle = build(directory, 'research', context=dict(laser_id=laser_id, task='study_evaluation',
        objective=objective, scope='offline_candidate_only', job_id=meta['id']),
        evidence=dict(files=manifest), max_input_bytes=32000)
    evidence = dict(files=manifest, bundle_revision=bundle['bundle_revision'],
                    instructions=bundle['instructions']+'\n\nStudy-specific task:\n'+objective)
    # This grant is an analysis-only core decision context with no physical
    # resources. It is never presented to an owner or used to acquire a lease.
    ctx = Context(Grant(laser_id, 'study-analysis', '1', 'no-physical-lease',
                        digest(manifest), meta['id'], frozenset()), clock()+timeout_s, 1, clock)
    journal = Journal(directory/'study-decision.sqlite3')
    admitted = False
    try:
        if not journal.admit('evaluation', laser_id, dict(evidence=manifest, actions=actions)):
            raise ValueError('Existing study decision requires inspection; no automatic replay')
        admitted = True
        journal.transition('evaluation', 0, 'running', {'kind':'evidence_published'})
        chosen = decide(provider or ResidentDecisions(directory, selected, clock=clock, sleep=sleep,
                        cancel=cancel, publish=publish), evidence, actions, ctx)
        if chosen.get('state') == 'waiting_for_decision':
            raise ValueError('No final study decision')
        for item in manifest:
            if assets.read(item['path'])['sha256'] != item['sha256']:
                raise ValueError('Study evidence copy changed during analysis')
        journal.transition('evaluation', 1, 'succeeded', {'kind':'proposal_validated'}, chosen)
        atomic_json(directory/'study-decision.json', dict(proposal=chosen, applied=False,
                    bundle_revision=bundle['bundle_revision'], evidence=manifest))
        return chosen
    except Exception as error:
        record = journal.get('evaluation') if admitted else None
        if record and record['state'] == 'running':
            journal.transition('evaluation', record['sequence'], 'cancelled' if isinstance(error, CardCancelled) else 'failed',
                               {'kind':'decision_failed'}, {'error_type':type(error).__name__, 'applied':False})
        raise
    finally:
        journal.close()
