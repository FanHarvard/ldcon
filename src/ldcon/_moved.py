"""Old import paths of modules moved into subpackages (refactor phase 2, 2026-10-27).

`ldcon.MOVED` maps each old name to the new dotted path. A meta-path finder installed by
`ldcon/__init__.py` resolves `import ldcon.<old>`, `from ldcon.<old> import x`, `from ldcon import <old>`,
`importlib.import_module('ldcon.<old>')` and `python -m ldcon.<old>` to the *same module object* as the new
path (an alias in `sys.modules`, not a copy), so a `monkeypatch.setattr` through the old alias reaches the
globals the real functions read. Entry modules additionally keep a tiny `ldcon/<old>.py` stub for
`python -m ldcon.<old>` (the units name them). The aliases exist for releases, units, deploy scripts and
external callers that still use the old names; code inside the package imports the new paths directly.
"""
import importlib, importlib.abc, importlib.machinery, importlib.util, sys


def alias(old, new=None):
    """Bind sys.modules[old] and the parent attribute to the module at `new` (default: ldcon.MOVED[old])."""
    new = new or sys.modules['ldcon'].MOVED[old]
    module = importlib.import_module(new)
    sys.modules[old] = module
    parent, _, attribute = old.rpartition('.')
    setattr(sys.modules[parent], attribute, module)
    return module


class _Loader(importlib.abc.Loader):
    def __init__(self, new): self.new = new
    def create_module(self, spec):
        module = importlib.import_module(self.new)
        self.spec = module.__spec__
        return module
    def exec_module(self, module):
        module.__spec__ = self.spec  # module_from_spec replaced it with the alias spec; the module keeps its own identity
    def get_code(self, name):  # runpy (`python -m ldcon.<old>`) without a stub file
        return importlib.import_module(self.new).__spec__.loader.get_code(self.new)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, table): self.table = table
    def find_spec(self, name, path=None, target=None):
        new = self.table.get(name)
        if new is None: return None
        found = importlib.util.find_spec(new)
        if found is None: raise ImportError(f'{name} was moved to {new}, which does not exist')
        return importlib.machinery.ModuleSpec(name, _Loader(new), origin=found.origin)


def install(table):
    """Append the finder once (after the path finder, so real files such as the entry stubs win)."""
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.append(_Finder(table))
