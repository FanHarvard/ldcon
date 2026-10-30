"""Agent/notification preferences only; no model or device operation routes."""
import time

from fastapi import Request, HTTPException
from fastapi.responses import FileResponse

from ldcon.agents import notification_policy
from ldcon.web.web_runtime import ConflictError


def notification_view(root):
    document = notification_policy.read_policy(root)
    now = time.time()
    lasers = {laser: notification_policy.resolve_policy(root, laser, now=now, document=document)
              for laser in document['lasers']}
    tasks = {task: dict(laser_id=value['laser_id'], completed=value.get('completed', False),
                        expires_unix_s=value.get('expires_unix_s'),
                        **notification_policy.resolve_policy(root, value['laser_id'], task, now, document=document))
             for task, value in document['tasks'].items()}
    return dict(document=document, revision=document['revision'],
                presets=list(notification_policy.PRESETS),
                resolved=dict(global_policy=dict(document['global_policy']), lasers=lasers, tasks=tasks),
                activation='Future notification events only; no lock restart or outbox replay.',
                checked_unix_s=now)


def install_routes(app, prefix, assets, body, *, root=None):
    def settings_root():return root if root is not None else app.state.runtime.root
    @app.get(prefix + '/agent-settings', include_in_schema=False)
    def page():
        return FileResponse(assets / 'agent-settings.html', media_type='text/html')

    @app.get(prefix + '/agent-settings-assets/{name}', include_in_schema=False)
    def asset(name: str):
        if name not in ('agent-settings.js', 'agent-settings.css'):
            raise HTTPException(404)
        return FileResponse(assets / name)

    @app.get(prefix + '/api/notification-policy')
    def notification_settings():
        return notification_view(settings_root())

    @app.put(prefix + '/api/notification-policy')
    async def notification_save(request: Request):
        value = await body(request)
        if set(value) != {'document', 'expected_revision'}:
            raise ValueError('Expected document and expected_revision')
        try:
            notification_policy.save_policy(settings_root(), value['document'], value['expected_revision'])
        except ValueError as error:
            if str(error) == 'Notification policy changed; refresh before saving':
                raise ConflictError(str(error)) from error
            raise
        return notification_view(settings_root())
