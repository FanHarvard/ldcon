"""Public retirement must not remove general PI tasks or historical evidence."""
from pathlib import Path

import pytest
import yaml

from ldcon.core.interface_cards import INTERFACE_CATALOG, LEGACY_INTERFACE_CATALOG
from ldcon.core.operation_registry import admission_class, normalize_parameters, spec, validate_card
from ldcon.jobs.pi_algorithms import ALGORITHMS, parameters

RETIRED = 'tune.pid_factor_reduction'


def test_factor_only_public_admission_is_retired_but_history_stays_readable():
    card = dict(id='old-pi', operation=RETIRED, uses=[], parameters={})
    assert RETIRED not in ALGORITHMS and RETIRED not in INTERFACE_CATALOG
    assert RETIRED in LEGACY_INTERFACE_CATALOG
    for admission in (
        lambda: spec(RETIRED), lambda: normalize_parameters(card, {}),
        lambda: validate_card({}, {}, card), lambda: admission_class({}, card),
    ):
        with pytest.raises(ValueError, match='Retired card'):
            admission()
    assert spec(RETIRED, historical=True).execution == 'retired'
    with pytest.raises(ValueError, match='fixed public PI algorithm'):
        parameters(card, {})


@pytest.mark.parametrize('family', ['ecdl-pzt-ldc-tec', 'dfb-dbr-ldc-tec'])
def test_general_pi_comparison_and_bidirectional_task_presets_remain(family):
    path = (Path(__file__).resolve().parent / 'support') / 'jobs/examples/interface-config/interface-decks' / (family+'.yaml')
    deck = yaml.safe_load(path.read_text())
    cards = {card['id']: card for card in deck['cards']}
    assert all(card['operation'] != RETIRED for card in cards.values())
    comparison = cards['pi-comparison']
    assert comparison['operation'] == 'measure.change'
    assert comparison['parameters']['changes'] == []
    assert comparison['parameters']['measurements']['wavelength'] is True
    task = cards['pi-optimization']
    assert task['operation'] == 'agent.task'
    assert task['parameters']['instruction_profile'] == 'adjustment'
    assert task['parameters']['adjustment_family'] == 'pi'
