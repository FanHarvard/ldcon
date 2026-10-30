"""Target changes remain surgical after Web configuration JSON saves."""
import copy
import json

import pytest
import yaml

from ldcon.core.contracts import DeviceError
from ldcon.jobs.lock_target import edit_target_text


@pytest.mark.parametrize('text', [
    '# keep\ntarget:\n  # operator note\n  frequency_hz: 274589113700000.03 # Hz\n  max_change_hz: 100000000.0\nother: 0.0\n',
    'target: {frequency_hz: 274589113700000.03, max_change_hz: 100000000.0}\nother: 0.0\n',
    "'target':\n  'frequency_hz': 274589113700000.03\nother: 0.0\n",
    json.dumps(dict(id='sr1092', devices={'native': {'zero': 0.0}},
                    target=dict(frequency_hz=274589113700000.03, max_change_hz=100000000.0)), indent=2),
])
def test_target_edit_preserves_every_other_byte(text):
    before = yaml.safe_load(text)
    expected = copy.deepcopy(before)
    expected['target']['frequency_hz'] = 274589143699999.97
    result = edit_target_text(text, expected['target']['frequency_hz'])
    assert yaml.safe_load(result) == expected
    assert result == text.replace('274589113700000.03', '274589143699999.97')


@pytest.mark.parametrize('text', [
    'target: {frequency_hz: 1, frequency_hz: 2}',
    'target: {frequency_hz: 1}\ntarget: {frequency_hz: 2}',
    'devices: {target: {frequency_hz: 1}}',
    'target: {frequency_hz: true}',
    'target: {frequency_hz: "123"}',
    'target: {frequency_hz: .nan}',
    'reference: &frequency 1\ntarget: {frequency_hz: *frequency}',
    'reference: &target {frequency_hz: 1}\ntarget: *target',
])
def test_ambiguous_or_nonnumeric_target_is_not_rewritten(text):
    with pytest.raises(DeviceError):
        edit_target_text(text, 3.)
