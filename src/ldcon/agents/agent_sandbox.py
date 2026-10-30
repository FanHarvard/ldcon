"""Read-only shared filesystem for resident CLI and all child tools.

The host controller/owners run outside this mount/PID namespace. There is no
fallback to an unrestricted agent when bubblewrap cannot establish protection.
"""
import os
import json
from pathlib import Path
from ldcon.core.paths import checkout_root


def command(argv, writable, readonly=()):
    roots=[]
    for value in writable:
        path=Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError('Sandbox writable roots must be real absolute directories')
        if path==Path('/') or str(path) in ('/opt','/opt/ldcon','/home'):
            raise ValueError('Sandbox writable scope is too broad')
        if path not in roots:roots.append(path)
    args=['/usr/bin/bwrap','--die-with-parent','--unshare-user','--unshare-pid',
          '--cap-drop','ALL','--ro-bind','/','/','--proc','/proc','--dev','/dev',
          '--tmpfs','/tmp']
    for path in roots:args+=['--bind',str(path),str(path)]
    # Applied last so a broad home/runtime bind cannot shadow shared code.
    for value in readonly:
        path=Path(value)
        if not path.is_absolute() or not path.exists():raise ValueError('Shared read-only root missing')
        args+=['--ro-bind',str(path),str(path)]
    return args+_private_mounts()+['--',*argv]


def _private_mounts():
    """Host signing keys must be absent, even though the agent shares its UID."""
    values=json.loads(os.environ.get('LDCON_PRIVATE_ROOTS','[]'))
    if not isinstance(values,list):raise ValueError('LDCON_PRIVATE_ROOTS must be a JSON array')
    # Host-only native gateway credentials must not reach model tools, including
    # tools running in another laser's sandbox. The HTTP child receives its own
    # password only during bootstrap and removes it from its tool environment.
    access=Path.home()/'.local/state/ldcon/gateway-access'
    if access.exists():values=[*values,str(access)]
    args=[]
    for value in values:
        if not isinstance(value,str):raise ValueError('Private root must be a path')
        root=Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir() or len(root.parts)<4:
            raise ValueError('Private root must be an existing, scoped directory')
        args+=['--tmpfs',str(root)]
    return args


def resident_command(argv, parameters):
    from ldcon.agents.agent_tools import Toolbox
    workspace=Path(parameters['workspace']).resolve()
    if parameters.get('compatibility_probe'):
        # Probes have no MCP context; they receive the same filesystem boundary.
        return command(argv,[Path.home(),workspace],[checkout_root()])
    box=Toolbox(parameters['tools_context'])
    root=box.root.resolve()
    if not workspace.is_relative_to(root):
        raise ValueError('Resident workspace must belong to its assigned laser deployment')
    if parameters.get('task_depth')==1:
        return task_command(argv,parameters,box)
    home=Path.home()
    writable=[root,home]
    from ldcon.core.site_bindings import path, shared_roots
    public=path('LDCON_AGENTBRIDGE_PUBLIC')
    shared=[*shared_roots(),checkout_root()]
    local=home/'.local'
    if local.is_dir():shared.append(local)
    units=home/'.config/systemd'
    if units.is_dir():shared.append(units)
    if any(path.resolve().is_relative_to(root) for path in shared):
        raise ValueError('Laser writable root must not contain shared code')
    # The deployment may lie below a shared root, so re-open only this laser after the
    # shared root. Releases/owner runtimes remain outside this writable subtree.
    base=command(argv,[home,*([public] if public.is_dir() else [])],shared)
    i=base.index('--')
    private=[]
    if parameters.get('provider')=='opencode':
        # ~/.local contains shared installed code and stays read-only, except
        # OpenCode's own conversation/state stores. Native auth paths are kept.
        for variable,default in (('XDG_DATA_HOME',home/'.local/share'),('XDG_STATE_HOME',home/'.local/state')):
            folder=Path(os.environ.get(variable) or default)/'opencode'
            if (not folder.is_absolute() or any(p.is_symlink() for p in (folder,*folder.parents))
                    or not any(folder.resolve().is_relative_to(base.resolve()) for base in (home,root))):
                raise ValueError('OpenCode data/state directories must stay inside its service-user home or laser deployment, without symlinks')
            folder.mkdir(parents=True,exist_ok=True)
            private+=['--bind',str(folder),str(folder)]
    # Apply masks again after the assigned deployment/private state bindings.
    return base[:i]+['--bind',str(root),str(root),*private,*_private_mounts()]+base[i:]


def task_command(argv,parameters,box):
    """Keep task admission, grants and receipts outside the child write mounts."""
    from ldcon.core.laser_assets import layout, AREAS, prepare_write_lock
    from ldcon.core.site_bindings import shared_roots
    from ldcon.agents.agent_profiles import location
    root=box.root.resolve();home=Path.home()
    paths=layout(root,box._laser_id)
    if Path(parameters['workspace'])!=Path(paths['laser_root']):
        raise ValueError('Task workspace must be its assigned laser assets')
    shared=[*shared_roots(),checkout_root()]
    for path in (home/'.local',home/'.config/systemd',location(root)):
        if path.exists():shared.append(path)
    native_name={'codex':'.codex','claude':'.claude','deepseek_harness':'.dsh'}.get(parameters['provider'])
    native=[]
    if native_name:
        value=home/native_name
        if value.is_symlink():raise ValueError('Task provider state must not be a symlink')
        value.mkdir(parents=True,exist_ok=True)
        native.append(value)
    base=command(argv,native,shared)
    i=base.index('--')
    mounts=['--ro-bind',str(root),str(root)]
    # All writers retain the same flock inode. Only that regular lock file and
    # the four content areas are writable; instructions and authority stay RO.
    asset_lock=prepare_write_lock(root,box._laser_id)
    mounts+=['--bind',str(asset_lock),str(asset_lock)]
    for value in [*(Path(paths[area]) for area in AREAS),box.directory/'agent-tool-state']:
        if (not value.is_dir() or any(p.is_symlink() for p in (value,*value.parents))
                or not value.resolve().is_relative_to(root)):
            raise ValueError('Task writable areas must be real scoped directories')
        mounts+=['--bind',str(value),str(value)]
    # Provider-native conversation stores remain writable, installed .local
    # packages do not. This repeats the resident's narrow OpenCode exception.
    if parameters['provider']=='opencode':
        for variable,default in (('XDG_DATA_HOME',home/'.local/share'),('XDG_STATE_HOME',home/'.local/state')):
            value=Path(os.environ.get(variable) or default)/'opencode'
            if (not value.is_absolute() or any(p.is_symlink() for p in (value,*value.parents))
                    or not value.resolve().is_relative_to(home) or value.resolve().is_relative_to(root)):
                raise ValueError('Task OpenCode state must stay inside its service-user home')
            value.mkdir(parents=True,exist_ok=True)
            mounts+=['--bind',str(value),str(value)]
    adaptive=os.environ.get('LDCON_ADAPTIVE_ROOT')
    if adaptive:
        private=Path(adaptive)
        if not private.is_absolute() or private.is_symlink() or not private.is_dir():
            raise ValueError('Adaptive authority root must be a real absolute directory')
        mounts+=['--tmpfs',str(private)]
    return base[:i]+mounts+_private_mounts()+base[i:]
