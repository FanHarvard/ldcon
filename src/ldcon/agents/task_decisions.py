"""One scoped decision session per card; the same model authors its report.

The child has evidence tools only. The existing card host owns all physical
decisions, grants, validation, adoption and interruption handling.
"""
import concurrent.futures
import json
import time
import uuid
from pathlib import Path

from experiment_core.records import atomic_json
from ldcon.core.records import read_json

TOOLS = frozenset({'agent_capability_probe', 'laser_files_read', 'laser_files_list',
    'laser_metrics', 'laser_owner_status', 'laser_operating_mode', 'laser_adjustment_guidance', 'laser_configuration_read',
    'laser_settings_read', 'laser_runtime_read', 'laser_cards', 'laser_active_jobs',
    'laser_card_job', 'laser_log_read', 'laser_history_read', 'laser_knowledge_read'})
CATALOG_TIMEOUT_S = 30


class TaskDecisions:
    def __init__(self, directory, selected, profile, *, clock, cancel, deadline,
                 adapter_factory=None):
        from ldcon.agents.resident_agent import prepare_context, register_bridge
        from ldcon.agents.agent_adapters import SessionAdapter
        from ldcon.agents.agent_bundle import build
        self.directory = Path(directory)
        self.selected, self.profile = selected, profile
        self.clock, self.cancel = clock, cancel
        self.deadline = min(deadline, clock()+profile['timeout_s'])
        self.closed, self.turns = False, 0
        self.adapter = None
        store, meta, key, parent = selected
        self.meta, self.parent = meta, parent
        if not set(profile['required_tools']) <= TOOLS:
            raise ValueError('Decision profile requires tools outside the analysis scope')
        if profile['max_turns'] < 3:
            raise ValueError('Decision profile needs probe, decision and explanation turns')
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        p = dict(provider=profile['provider'], model=profile['model'], effort=profile['effort'],
                 workspace='', remote_control=False, request_timeout_s=self.remaining())
        p = prepare_context(directory, p, store, meta, 'task-'+meta['id'], task_context=True)
        # Registration is trusted host bootstrap, before a model receives any
        # tools. Do not grant bridge mutation/inbox tools to the decision model.
        registration = register_bridge(p)
        atomic_json(self.directory/'bridge-registration.json', registration)
        context_path = Path(p['tools_context'])
        context = read_json(context_path)
        self.catalog_nonce = uuid.uuid4().hex
        context.update(task_depth=1, task_scope='read_only', task_id=meta['id'],
                       task_tool_catalog_nonce=self.catalog_nonce,
                       task_allowed_tools=sorted(TOOLS))
        atomic_json(context_path, context)
        # ACP indexes tools by working directory; never replace resident tools.
        transport = self.directory/'agent-tool-state'/'opencode-cwd'
        if any(x.is_symlink() for x in (transport, *transport.parents)):
            raise ValueError('Task transport must not use symlinks')
        transport.mkdir(parents=True, exist_ok=True)
        p['task_depth']=1
        if p['provider']=='opencode':p['transport_workspace'] = str(transport.absolute())
        bundle = build(directory, 'research', context=dict(laser_id=p['laser_id'],
            task_id=meta['id'], parent_card=json.loads(key)[2], profile=profile,
            scope='host_executed_decisions'), evidence={}, max_input_bytes=profile['max_input_bytes'])
        self.bundle_revision = bundle['bundle_revision']
        self.laser_id = p['laser_id']
        from experiment_core.agent_roles import role_contract
        p['instructions'] = bundle['instructions']+'\n'+p['instructions']+'\n'+role_contract('task')['instructions']+(
            '\nYou are the card task decision and explanation author. The resident is the communication '
            'parent, not your reviewer or report writer. Use only advertised read-only evidence tools. '
            'Return the requested structured decisions; the card host executes and verifies operations. '
            'Never issue physical commands, change settings, spawn agents, poll inboxes or send notifications. '
            'Preserve user/foreground priority. A proposal is not an applied result. '
            'Write the final explanation yourself from the final host receipts, including failures and limits. '
            'Use English for internal results and automatic reports.')
        p['instructions_resolved'] = True
        from experiment_core.agent_roles import relationship
        self.relationship=relationship(meta['id'],gateway_id=parent['job'],
            parent_session=parent['session'],profile=profile)
        atomic_json(self.directory/'task-decision-context.json', dict(
            **self.relationship,bundle_revision=self.bundle_revision))
        atomic_json(self.directory/'agent-effective-instructions.json', dict(
            text=p['instructions'], bundle_revision=self.bundle_revision))
        self.adapter = (adapter_factory or SessionAdapter)(p, self.is_cancelled)

    def remaining(self):
        return max(.01, self.deadline-self.clock())

    def is_cancelled(self):
        return self.closed or self.cancel() or self.clock() >= self.deadline

    def phase(self, name):
        atomic_json(self.directory/'agent-task-phase.json', dict(task_id=self.meta['id'],
            laser_id=self.laser_id, depth=1, phase=name, allowed_tools=sorted(TOOLS),
            expires_unix_s=time.time()+self.remaining()))

    def call(self, function, tick):
        future = self.pool.submit(function)
        try:
            while True:
                tick()
                if self.is_cancelled():
                    from ldcon.core.interface_cards import CardCancelled
                    raise CardCancelled('Task cancelled or computation deadline reached')
                try:
                    return future.result(timeout=.1)
                except concurrent.futures.TimeoutError:
                    if future.done():
                        return future.result()
        except BaseException:
            self.closed = True
            self.adapter.interrupt()
            raise

    def connect(self, tick=lambda: None):
        self.phase('probe')
        self.session = self.call(lambda: self.adapter.connect(''), tick)
        info = self.adapter.model_info
        if (info.get('confirmation') != 'server_reported' or
                info.get('resolved_model') != self.profile['model'] or
                (self.profile['effort'] and info.get('resolved_effort') != self.profile['effort'])):
            raise ValueError('Task model/effort not confirmed; no silent fallback')
        # ACP session creation can return before the provider has loaded MCP.
        # Wait for its tools/list request, while still honoring foreground work.
        # The following model-originated probe remains mandatory.
        def wait_catalog():
            deadline=time.monotonic()+min(CATALOG_TIMEOUT_S,self.remaining())
            while time.monotonic()<deadline and not self.is_cancelled():
                catalog=read_json(self.directory/'agent-tool-state'/'agent-tool-catalog.json',{})
                if (catalog.get('nonce')==self.catalog_nonce and
                        catalog.get('task_id')==self.meta['id'] and
                        catalog.get('laser_id')==self.laser_id and
                        set(catalog.get('tools',[]))==TOOLS):
                    return
                time.sleep(.05)
            raise ValueError('Scoped task MCP catalog was not loaded before the readiness deadline')
        self.call(wait_catalog,tick)
        nonce = uuid.uuid4().hex
        atomic_json(self.directory/'agent-probe-challenge.json', dict(nonce=nonce,
            bundle_revision=self.bundle_revision))
        self.request(dict(kind='capability_probe', prompt='Call agent_capability_probe once with nonce '+nonce+
            '. Use the advertised ldcon MCP namespace, including Code Mode discovery if the provider '
            'exposes tools through it. Do not substitute a shell command or another capability. '
            'No other actions. Return a short confirmation.'), tick=tick, reserve_report=False)
        receipt = read_json(self.directory/'agent-probe-receipt.json', {})
        if (receipt.get('nonce') != nonce or receipt.get('laser_id') != self.laser_id or
                receipt.get('bundle_revision') != self.bundle_revision or
                not set(self.profile['required_tools']) <= set(receipt.get('tools', []))):
            raise ValueError('Scoped task evidence-tool probe did not complete')
        self.phase('work')
        atomic_json(self.directory/'task-decision-session.json', dict(session=self.session,
            profile=self.profile, model_info=dict(info), parent=self.parent['job'],
            report_author='task_model'))
        return self

    def request(self, payload, *, tick=lambda: None, reserve_report=True):
        from ldcon.core.interface_schema import load_mapping
        from ldcon.agents.operating_mode_client import observation as observe_mode
        mode = observe_mode(load_mapping((self.directory/'laser.yaml').read_text()))
        from ldcon.agents.adjustment_guidance import for_job
        guidance=for_job(self.directory,self.laser_id)
        payload = {**payload, 'operating_mode_at_turn': mode, 'adjustment_guidance_at_turn': guidance}
        if self.turns >= self.profile['max_turns']-int(reserve_report):
            raise ValueError('Task decision budget exhausted; report turn reserved')
        if len(json.dumps(payload).encode()) > self.profile['max_input_bytes']:
            raise ValueError('Task input exceeds profile budget; evidence must be chunked')
        self.turns += 1
        from ldcon.agents.agent_bundle import turn_origin
        turn_origin(self.directory,'child-turn-'+str(self.turns),payload.get('kind','card_task'),task_id=self.meta['id'])
        self.adapter.p['request_timeout_s'] = self.remaining()
        atomic_json(self.directory/('task-turn-%d-input.json'%self.turns), payload)
        answer = self.call(lambda: self.adapter.request(payload), tick)
        atomic_json(self.directory/('task-turn-%d-answer.json'%self.turns), dict(answer=answer,
            model_info=dict(self.adapter.model_info)))
        return answer

    def propose(self, evidence, actions, budget):
        from ldcon.jobs.study_execution import parse_proposal
        deadline=self.deadline
        from experiment_core.agent_roles import decision_deadline
        self.deadline=decision_deadline(deadline,budget['deadline'])
        try:
            answer=self.request(dict(kind='study_evaluation', evidence=evidence, allowed_actions=actions,
                prompt=evidence['instructions']+' Return only JSON with exactly action and parameters. Read every listed evidence file before deciding.'))
            return parse_proposal(answer)
        finally:self.deadline=deadline

    def explain(self, result):
        """One final author turn after receipts; failure never erases adoption."""
        try:
            from ldcon.agents.resident_adjustment import mailbox_payload
            from ldcon.core.laser_assets import LaserAssets
            store = self.selected[0]
            payload = mailbox_payload(LaserAssets(store.root.parent, self.laser_id),
                'data/task-report-'+self.meta['id'], dict(kind='task_explanation', job=self.meta['id'],
                prompt='Write the final concise English explanation for the human. State what was requested, '
                    'what was measured, what was actually adopted or restored, the rationale, and remaining '
                    'uncertainty. Explicitly distinguish saved/pending from owner-confirmed. Do not propose '
                    'or perform further actions. Your text will be relayed verbatim by the resident.', evidence=result))
            answer = self.request(payload, reserve_report=False)
            from experiment_core.agent_roles import explanation
            report=explanation(answer,task_id=self.meta['id'],session=self.session,
                profile=self.profile,model_info=self.adapter.model_info)
        except Exception as error:
            from experiment_core.agent_roles import explanation_unavailable
            report=explanation_unavailable(error)
        atomic_json(self.directory/'task-explanation.json', report)
        return report

    def close(self):
        self.closed = True
        try:
            self.phase('closed')
            if self.adapter:
                self.adapter.interrupt()
                self.adapter.close()
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
