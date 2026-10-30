"""Read grants for shared code and assigned configuration; never grants edits."""
from pathlib import Path


SHARED_CODE_INSTRUCTIONS = ('Read the actual installed shared source before implementation: '
    'Use the configured immutable source roots and their installed version metadata. '
    'Distinguish the current card release from older pinned owner code. State the exact path/version '
    'and functions inspected. Subagent reports do not replace direct source verification. '
    'These are Read grants only; never edit a shared immutable release, bypass a denied tool, '
    'or treat source access as permission to change hardware or safety limits.')


def read_rules(parameters):
    from ldcon.core.site_bindings import shared_roots
    rules = ['Read(/' + str(root) + '/**)' for root in shared_roots()]
    for name in parameters.get('config_read_paths', []):
        path = Path(name)
        # Claude permission syntax treats these characters as patterns/separators.
        if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '*?[](),\n\r'):
            raise ValueError('Expected a literal absolute configuration path')
        rules.append('Read(/' + str(path) + ')')
    return rules
