"""Browser numeric serialization cannot fabricate forbidden device edits."""
import copy
import json
from types import SimpleNamespace

import pytest

from ldcon.core.agent_authority import changes, describe_changes
from ldcon.core.configuration import preserve_numeric_types, _check_active
from ldcon.core.interface_schema import digest
from ldcon.web.web_runtime import ConflictError


def fixture():
    return {'id':'laser','target':{'frequency_hz':274589113700000.03},'devices':{'native':{
        'control_settings':{'native_current':{'adc':{'min_voltage':0.0},
            'calibration':{'zero_voltage_v':0.0}, 'current_bounds_a':[0.0,0.1],
            'native':{'integral_limit_count':45.0,'kp_count_per_nm':-100.0}}}}}}


def browser_numbers(value):
    if isinstance(value,dict):return {k:browser_numbers(v) for k,v in value.items()}
    if isinstance(value,list):return [browser_numbers(v) for v in value]
    return int(value) if type(value) is float and value.is_integer() else value


def test_only_real_integral_edit_is_live_and_operational():
    before=fixture();after=browser_numbers(before)
    after['devices']['native']['control_settings']['native_current']['native']['integral_limit_count']=100
    rows=describe_changes('lasers',before,after)
    assert len(rows)==1 and rows[0]['path'].endswith('.native.integral_limit_count')
    assert rows[0]['class']=='free' and rows[0]['live']=='native_hot'
    _check_active(SimpleNamespace(),[{'laser':{'id':'laser'}}],'lasers','laser',after,rows)
    normalized=preserve_numeric_types(before,after)
    expected=copy.deepcopy(before)
    expected['devices']['native']['control_settings']['native_current']['native']['integral_limit_count']=100.0
    assert json.dumps(normalized)==json.dumps(expected)
    assert digest(normalized)==digest(expected)
    # The same browser route must still block a real calibration change.
    after['devices']['native']['control_settings']['native_current']['calibration']['zero_voltage_v']=1
    rows=describe_changes('lasers',before,after)
    with pytest.raises(ConflictError,match='active card'):
        _check_active(SimpleNamespace(),[{'laser':{'id':'laser'}}],'lasers','laser',after,rows)


@pytest.mark.parametrize('a,b',[(True,1),(False,0),(1,'1'),(1.0,1.0000000000000002)])
def test_real_or_typed_changes_are_not_hidden(a,b):
    assert changes(a,b)==[()]


def test_no_rounding_during_type_preservation():
    assert type(preserve_numeric_types(1,1.5)) is float
    assert preserve_numeric_types(1.0,2**53+1)==2**53+1
    assert type(preserve_numeric_types(1.0,2**53+1)) is int
    assert type(preserve_numeric_types(0.0,False)) is bool
    before=fixture()
    assert describe_changes('lasers',before,browser_numbers(before))==[]
    assert digest(preserve_numeric_types(before,browser_numbers(before)))==digest(before)
