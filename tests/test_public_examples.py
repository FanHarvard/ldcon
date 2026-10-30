import re

import pytest
import yaml

from ldcon.example_config import export
from ldcon.core.interface_schema import InterfaceRepository


def test_export_is_valid_disabled_and_has_no_live_endpoints(tmp_path):
    root = export(tmp_path / 'examples')
    repository = InterfaceRepository(root)
    for name in ('sr422', 'sr1092', 'thf685'):
        definition = repository.assignment(name)
        assert definition['laser_definition']['document']['allow_output'] is False
        assert definition['deck_definitions']
    for path in root.rglob('*.yaml'):
        text = path.read_text()
        assert not re.search(r'\b(?:192\.168\.\d+\.\d+|127\.0\.0\.1)\b', text)
        assert not re.search(r'(?im)^\s*(?:password|token|api_key|secret|webhook_url):', text)
        assert isinstance(yaml.safe_load(text), dict)


def test_export_never_overwrites_existing_configuration(tmp_path):
    root = tmp_path / 'existing'
    root.mkdir()
    sentinel = root / 'local.yaml'
    sentinel.write_text('keep: true\n')
    with pytest.raises(FileExistsError):
        export(root)
    assert sentinel.read_text() == 'keep: true\n'
    assert list(root.iterdir()) == [sentinel]
