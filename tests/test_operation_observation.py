"""Discovery observes configured owner ports without claiming or changing them."""
from ldcon.web.operation_observation import describe_owners


def test_native_discovery_keeps_configured_and_effective_separate():
    calls = []
    laser = dict(id='fixture', devices={'native':dict(type='esp32servocon.native_current.owner',
                 connection=dict(endpoint='http://configured.invalid:1',device_id='physical-1'))})
    def rpc(endpoint, request, **kwargs):
        calls.append((endpoint, request))
        if request['action']=='describe':
            return dict(device_id='physical-1', policy_digest='effective-owner-revision', capabilities=['retarget'])
        return dict(native_pi_active=True, leased=True, fault=None, lease='never-exposed',
                    read_ok=False,last_success_unix_s=100.)
    result = describe_owners(laser, rpc=rpc)
    assert result['status']=='observed'
    assert result['effective_policy_revision']=={'native':'effective-owner-revision'}
    assert result['observed'][0]['observed']['native_pi_active'] is True
    assert 'lease' not in result['observed'][0]['observed']
    assert 'received_unix_s' in result['observed'][0] and 'observed_unix_s' not in result['observed'][0]
    assert result['observed'][0]['observed']['last_success_unix_s']==100.
    assert result['observed'][0]['observed']['read_ok'] is False
    assert {request['action'] for _,request in calls} == {'describe','status'}
    assert all(endpoint=='http://configured.invalid:1' for endpoint,_ in calls)


def test_ecdl_discovery_uses_bound_rig_and_refuses_wrong_identity():
    calls = []
    laser = dict(id='fixture',devices={'rig':dict(type='ecdl.rig.owner',connection=dict(endpoint='http://configured.invalid:1'))})
    def rpc(endpoint, request, **kwargs):
        calls.append(request)
        return dict(laser_id='wrong-laser',revision='cannot-use')
    result = describe_owners(laser,rpc=rpc)
    assert result['status']=='unavailable'
    assert result['effective_policy_revision']=={'rig':None}
    assert calls==[dict(action='ecdl',instrument='fixture',request=dict(action='describe'))]


def test_unreachable_owner_does_not_relabel_configured_policy_as_effective():
    laser=dict(id='fixture',devices={'native':dict(type='esp32servocon.native_current.owner',
          connection=dict(endpoint='http://configured.invalid:1',device_id='physical-1'))})
    calls=[]
    def rpc(*args,**kwargs):
        calls.append(args)
        raise ConnectionError('owner unavailable')
    result=describe_owners(laser,rpc=rpc)
    assert result['status']=='unavailable' and result['effective_policy_revision']=={'native':None}
    assert len(calls)==1
