"""Compatibility HTTP routes over the shared operation gateway."""
import asyncio


def install_routes(app, prefix, body):
    @app.get(prefix+'/api/jobs/{job_id}/window-actions')
    def status(job_id: str):
        return app.state.runtime.window_status(job_id)

    from fastapi import Request
    @app.post(prefix+'/api/jobs/{job_id}/window-actions', status_code=202)
    async def request(job_id: str, request: Request):
        return await asyncio.to_thread(app.state.runtime.request_window, job_id, await body(request))

    @app.post(prefix+'/api/jobs/{job_id}/window-agent-actions', status_code=202)
    async def agent_request(job_id: str, request: Request):
        # This route only permits the currently approved exact agent capability.
        return await asyncio.to_thread(app.state.runtime.request_window, job_id, await body(request), source='agent')

    @app.patch(prefix+'/api/jobs/{job_id}/window-actions/{request_id}')
    async def amend(job_id: str, request_id: str, request: Request):
        return await asyncio.to_thread(app.state.runtime.amend_window, job_id, request_id, await body(request))

    @app.post(prefix+'/api/jobs/{job_id}/window-actions/{request_id}/cancel')
    async def cancel(job_id: str, request_id: str, request: Request):
        data = await body(request)
        return await asyncio.to_thread(app.state.runtime.cancel_window, job_id, request_id,
                                       expected_revision=data.get('expected_revision'))
