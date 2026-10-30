from pathlib import Path

import pytest
import yaml
from ldcon.jobs.measurement_presets import cards,card,describe
from ldcon.jobs.change_measure import parameters, batch_parameters
from ldcon.jobs.measurement_analysis import local_slope,branch_segments,feedforward_estimate


def test_presets_have_no_implicit_physical_trials():
    for family in ('ecdl','dfb-dbr','ecdl-dlcpro'):
        for item in cards(family):
            if item['operation']=='measure.batch':continue
            p=parameters(item,{})
            assert p['changes']==[]
            assert not p['measurements']['dither']
    assert all(not c['parameters']['measurements']['optical_quality'] for c in cards('dfb-dbr') if c['operation']=='measure.change')


def test_all_presets_are_one_algorithm_and_ff_not_missing():
    assert {x['operation'] for x in describe()}=={'measure.change'}
    assert {'tec-current-feedforward','piezo-current-feedforward'} <= {x['id'] for x in describe()}
    with pytest.raises(ValueError):card('piezo-response',family='dfb-dbr')


def record(values):
    return {'steps':[{'status':'succeeded','application':{'readback':{'current':x}},'summary':{'wavelength':{'statistics':{'frequency_hz':{'mean':f}}}}} for x,f in values]}


def test_frequency_fit_never_fits_across_hop():
    good=record([(0,1e14),(1,1e14+2),(2,1e14+4)])
    fit=local_slope(good,'current',discontinuity_hz=100)
    assert fit['frequency_hz_per_unit']==2
    assert feedforward_estimate(fit,fit)['coefficient']==-1
    bad=record([(0,1e14),(1,1e14+2),(2,1e14+1e6)])
    assert local_slope(bad,'current',discontinuity_hz=100)['status']=='inconclusive'
    assert len(branch_segments(bad,'current',discontinuity_hz=100)['segments'])==2


@pytest.mark.parametrize('relative', [
    'examples/measurement-decks/ecdl-pzt-ldc-tec.yaml',
    'examples/measurement-decks/dfb-dbr-ldc-tec.yaml',
    'examples/dlcpro/deck.yaml',
    'jobs/examples/interface-config/interface-decks/ecdl-pzt-ldc-tec.yaml',
    'jobs/examples/interface-config/interface-decks/dfb-dbr-ldc-tec.yaml',
    'web/examples/interface-config/interface-decks/ecdl-pzt-ldc-tec.yaml',
    'web/examples/interface-config/interface-decks/dfb-dbr-ldc-tec.yaml',
])
def test_published_measurement_examples_supply_all_execution_values(relative):
    deck = yaml.safe_load(((Path(__file__).resolve().parent / 'support') / relative).read_text())
    found = 0
    for item in deck['cards']:
        if item['operation'] == 'measure.change':
            original = item['parameters']
            p = parameters(item, {})
            assert 'history_wait_s' in original
            assert all(key in original for key in p if key != 'target_frequency_hz')
            found += 1
        elif item['operation'] == 'measure.batch':
            assert batch_parameters(item, {}) == item['parameters']
    assert found
