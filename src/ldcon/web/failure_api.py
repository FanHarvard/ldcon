"""Operator-only failure review and consultation routes; no hardware actions."""
import asyncio

from fastapi import Request
from fastapi.responses import FileResponse

from ldcon.agents import failure_policy
from ldcon.agents.resident_agent import AgentStore
from ldcon.web.web_runtime import ConflictError


def install_routes(app, prefix, assets, body):
    @app.get(prefix+'/failures', include_in_schema=False)
    def page():
        return FileResponse(assets/'failures.html', media_type='text/html')

    @app.get(prefix+'/api/failures')
    def cases(laser_id: str = ''):
        runtime = app.state.runtime
        if laser_id:
            runtime.interface_repository.read('lasers', laser_id)
        return {'cases': failure_policy.list_cases(runtime.root, laser_id=laser_id or None)}

    @app.get(prefix+'/api/failures/{case_id}')
    def detail(case_id: str):
        root = app.state.runtime.root
        value = failure_policy.get_case(root, case_id)
        return {'case': value, 'questions': AgentStore(root).consultations(case_id=case_id)}

    @app.post(prefix+'/api/failures/{case_id}/questions/{question_id}/answer')
    async def answer(case_id: str, question_id: str, request: Request):
        data = await body(request)
        if set(data) != {'answer'}:
            raise ValueError('Expected the human answer')

        def save():
            runtime = app.state.runtime
            with runtime.mutex:
                runtime.assert_admission_open()
                value = failure_policy.get_case(runtime.root, case_id)
                if value.get('disposition') == 'resolved':
                    raise ConflictError('This failure is resolved; the previous question is closed')
                store = AgentStore(runtime.root)
                question = store.consultation(question_id)
                if question['case_id'] != case_id or question['key'] != value['agent_key']:
                    raise ValueError('Question does not belong to this failure and agent')
                return store.answer_consultation(question_id, data['answer'])
        return await asyncio.to_thread(save)

    @app.post(prefix+'/api/failures/{case_id}/verify')
    async def verify(case_id: str, request: Request):
        data = await body(request)
        if set(data) != {'replacement_job_id', 'note'}:
            raise ValueError('Expected a replacement execution and verification note')

        def check():
            runtime = app.state.runtime
            with runtime.mutex:
                runtime.assert_admission_open()
                return failure_policy.verify_case(runtime.root, case_id,
                    data['replacement_job_id'], data['note'])
        return await asyncio.to_thread(check)
