"""Owner-held operating intent and narrow, protected operator request intake.

The Web host authenticates the operator and writes an immutable artifact in the
trusted adaptive root. RPC callers supply only its ID and digest. Neither a
lease nor a model-authored authorization claim can change operating mode.
"""
import json
import os
from pathlib import Path
import re
import stat

from experiment_core.errors import AuthorizationRequired, OperationError
from experiment_core.operating_mode import (
    OperatingModeAuthority, OperatingModeAuthorization, validate_change_request)
from experiment_core.records import digest, safe_id

from ldcon.core.instrument_client import BrokerError

MODE_ACTIONS = frozenset({'operating_mode_status', 'operating_mode_change'})
BACKGROUND_EFFECTS = frozenset({'background_current', 'background_adopt', 'background_pid_target',
                                'background_parameters_target'})


class OperatingModeOwner:
    def __init__(self, journal, laser_id, *, coordinator, interrupt, persist=lambda: None):
        self.authority = OperatingModeAuthority(journal, laser_id)
        self.laser_id = self.authority.laser_id
        self.coordinator, self.interrupt, self.persist = coordinator, interrupt, persist

    def status(self):
        # This read never takes the hardware mutex. Background interruption is
        # informational; changes and admissions recheck under the owner mutex.
        state = self.authority.status()
        bg = self.coordinator()
        epoch = bg.epoch if bg is not None else None
        interruption = dict(pending=False)
        if epoch and (epoch.get('operating_mode_revision') != state['revision']
                      or epoch.get('reason') == 'operating_mode_changed'):
            interruption.update(pending=True, family=epoch.get('family'), state=epoch['state'],
                                reason=epoch.get('reason'), mode_revision=epoch.get('operating_mode_revision'))
        return dict(state, background_interruption=interruption)

    def _artifact(self, request):
        allowed = {'action', 'request_id', 'request_digest', 'device_id'}
        if set(request) - allowed or not {'request_id', 'request_digest'} <= set(request):
            raise ValueError('Mode RPC accepts only the authenticated request ID and digest')
        identity = safe_id(request['request_id'])
        expected = request['request_digest']
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid operating-mode request digest')
        root = Path(os.environ.get('LDCON_ADAPTIVE_ROOT', ''))
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            raise AuthorizationRequired('A protected adaptive authority root is required')
        folder = root / self.laser_id / 'operating-mode' / 'requests'
        path = folder / (identity + '.json')
        if any(item.is_symlink() for item in (root / self.laser_id, folder.parent, folder, path)):
            raise AuthorizationRequired('Operating-mode authorization must not use symlinks')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384:
                raise AuthorizationRequired('A bounded regular authorization artifact is required')
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise AuthorizationRequired('Operating-mode authorization is too large')
        value = validate_change_request(json.loads(raw))
        if value['laser_id'] != self.laser_id or value['request_id'] != identity or digest(value) != expected:
            raise AuthorizationRequired('Operating-mode authorization scope or digest differs')
        return value, OperatingModeAuthorization(expected, value['authenticated_operator'])

    def dispatch(self, request):
        try:
            if request['action'] == 'operating_mode_status':
                return self.status()
            if request['action'] != 'operating_mode_change':
                raise ValueError('Unknown operating-mode operation')
            value, authorization = self._artifact(request)
            result = self.authority.change(value, authorization)
            if result['changed'] and not result['idempotent_replay']:
                # A dispatch mutex held by the enclosing owner serializes this
                # memory-only fence with every physical effect and adoption.
                self.interrupt('operating_mode_changed')
                self.persist()
            else:
                self.reconcile()
            return dict(result, current_operating_mode=self.status())
        except AuthorizationRequired as error:
            raise BrokerError('authorization', str(error)) from error
        except (OperationError, ValueError, OSError) as error:
            raise BrokerError('configuration', str(error)) from error

    def reconcile(self):
        """A persisted change must fence even if its original RPC was interrupted."""
        bg = self.coordinator()
        if bg is None or bg.epoch is None:
            return
        if (bg.epoch.get('operating_mode_revision') != self.authority.status()['revision']
                and bg.epoch['state'] == 'active'):
            self.interrupt('operating_mode_changed')
            self.persist()

    def before(self, request):
        try:
            self.reconcile()
            action = request.get('action')
            if action == 'background_begin':
                self.authority.require_revision(request.get('expected_operating_mode_revision'))
            elif (action in BACKGROUND_EFFECTS or
                  action == 'background_pid_finish' and request.get('status') == 'succeeded'):
                bg = self.coordinator()
                if bg is not None and bg.epoch is not None:
                    self.authority.require_revision(bg.epoch.get('operating_mode_revision'))
        except OperationError as error:
            raise BrokerError('configuration', str(error)) from error

    def after(self, request, result):
        if request.get('action') == 'background_begin':
            bg = self.coordinator()
            if bg is not None and bg.epoch is not None:
                revision = self.authority.require_revision(request.get('expected_operating_mode_revision'))['revision']
                bg.epoch['operating_mode_revision'] = revision
                self.persist()
                if isinstance(result, dict) and isinstance(result.get('epoch'), dict):
                    result['epoch']['operating_mode_revision'] = revision
        if request.get('action') in ('background_status', 'status', 'describe', 'inspect') and isinstance(result, dict):
            result['operating_mode'] = self.status()
        return result
