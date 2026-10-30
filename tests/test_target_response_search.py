"""Pure proposal policy: no owner, target, model or configuration operations."""
import copy

import pytest

from ldcon.jobs.target_response_search import plan


def identification(scale=1.2, uncertainty=.02):
    return dict(status='static_candidate_only', target_feedforward_scale=scale,
        trials=[dict(status='static_estimate',scale=scale,uncertainty_scale=uncertainty) for _ in range(2)])


def round_trip(scale=1., step=100e6, times=(4., 4.5), *, fit=None):
    return dict(scale=scale,step_hz=step,status='completed',criteria_revision='fixed',
        trials=[dict(direction=direction,status='completed',history=dict(integrity_errors=[]),
            analysis=dict(status='settled' if time is not None else 'not_settled',
                settling_from_request_s=time)) for direction,time in zip(('out','back'),times)],
        feedforward_identification=fit or dict(status='inconclusive',trials=[]))


def decide(rounds, **kw):
    return plan(rounds,criteria_revision='fixed',**kw)


def test_starts_at_largest_requested_step_with_existing_scale():
    result=decide([],initial_scale=1.1)
    assert result['action']=='propose_trial'
    assert result['parameters']==dict(scale=1.1,step_hz=100e6)
    assert result['goal_s']==3. and result['remaining_roundtrips']==12


def test_exact_three_seconds_passes_both_legs_and_never_shrinks_100mhz():
    result=decide([round_trip(times=(3.,3.))])
    assert result['action']=='select' and result['parameters']['step_hz']==100e6
    assert result['worst_settle_s']==3.
    failed=decide([round_trip(times=(3.,3.0000001),fit=identification())])
    assert failed['action']=='propose_trial' and failed['parameters']['scale']==1.2
    assert failed['parameters']['step_hz']==100e6


def test_static_ff_is_tested_before_step_reduction():
    baseline=round_trip(fit=identification())
    first=decide([baseline]);assert first['parameters']==dict(scale=1.2,step_hz=100e6)
    measured=round_trip(scale=1.2,times=(3.2,3.4),fit=identification())
    second=decide([baseline,measured])
    assert second['action']=='propose_trial' and second['parameters']==dict(scale=1.2,step_hz=50e6)


def test_estimate_consistent_with_measured_scale_is_ff_evidence_not_a_blind_grid():
    result=decide([round_trip(fit=identification(1.))])
    assert result['parameters']==dict(scale=1.,step_hz=50e6)


@pytest.mark.parametrize('damage',['missing_return','quality_invalid','history_gap','inconclusive','duplicate_direction','nonfinite'])
def test_unqualified_round_cannot_pass_or_justify_a_smaller_step(damage):
    row=round_trip(times=(1.,1.),fit=identification())
    if damage=='missing_return':row['trials'].pop()
    elif damage=='quality_invalid':row['trials'][1]['analysis']['quality_valid']=False
    elif damage=='history_gap':row['trials'][0]['history']['integrity_errors']=['gap']
    elif damage=='inconclusive':row['trials'][0]['analysis']['status']='inconclusive'
    elif damage=='duplicate_direction':row['trials'][1]['direction']='out'
    else:row['trials'][1]['analysis']['settling_from_request_s']=float('nan')
    result=decide([row])
    assert result['action']=='propose_trial' and result['parameters']==dict(scale=1.,step_hz=100e6)
    assert decide([row,copy.deepcopy(row)])['action']=='inconclusive'


def test_history_gap_after_ff_trial_repeats_same_point_without_shrinking():
    first=round_trip(fit=identification())
    second=round_trip(scale=1.2,fit=identification())
    second['trials'][0]['analysis']['status']='inconclusive'
    result=decide([first,second])
    assert result['parameters']==dict(scale=1.2,step_hz=100e6)


@pytest.mark.parametrize('status',['denied','rejected','unknown','uncertain_write','failed','cancelled','preempted'])
def test_denied_or_unknown_operation_aborts_even_if_an_earlier_candidate_passed(status):
    row=round_trip();row['status']=status
    result=decide([round_trip(times=(1.,1.)),row])
    assert result['action']=='inconclusive' and result['parameters']=={}
    assert 'Terminal' in result['reason']


def test_nested_unknown_command_is_not_hidden_by_completed_analysis():
    row=round_trip(times=(1.,1.));row['trials'][1]['command']=dict(status='unknown')
    assert decide([row])['action']=='inconclusive'


def test_selection_uses_largest_step_then_worst_direction_not_average_or_latest():
    rows=[round_trip(step=50e6,times=(.2,.2)),round_trip(scale=1.1,step=75e6,times=(.1,2.9)),
          round_trip(scale=1.2,step=75e6,times=(2.5,2.5)),round_trip(scale=1.3,step=75e6,times=(2.8,2.8))]
    result=decide(rows,elapsed_s=1200)
    assert result['action']=='select' and result['round_index']==2
    assert result['parameters']==dict(scale=1.2,step_hz=75e6) and result['worst_settle_s']==2.5


def test_larger_passing_step_is_refined_against_measured_failure_adaptively():
    rows=[round_trip(scale=1.2,fit=identification()),
          round_trip(scale=1.2,step=50e6,times=(2.5,2.5),fit=identification())]
    result=decide(rows)
    assert result['parameters']==dict(scale=1.2,step_hz=75e6)
    rows.append(round_trip(scale=1.2,step=75e6,times=(3.2,3.1),fit=identification()))
    assert decide(rows)['parameters']==dict(scale=1.2,step_hz=62.5e6)


def test_no_static_or_dynamic_ff_evidence_does_not_invent_a_scale_grid_or_shrink():
    result=decide([round_trip()])
    assert result['action']=='inconclusive'
    assert 'no qualified static FF estimate' in result['reason']


def test_fallback_needs_measured_improvement_and_has_one_bounded_continuation():
    rows=[round_trip(scale=1.,times=(6.,6.)),round_trip(scale=1.2,times=(4.,4.))]
    result=decide(rows)
    assert result['parameters']['scale']==pytest.approx(1.4)
    assert result['parameters']['step_hz']==100e6
    rows.append(round_trip(scale=1.4,times=(3.5,3.5)))
    assert decide(rows)['parameters']==dict(scale=1.4,step_hz=50e6)
    rows=[round_trip(scale=1.,times=(4.,4.)),round_trip(scale=1.2,times=(4.,4.))]
    assert decide(rows)['parameters']['step_hz']==50e6


def test_conflicting_static_directions_are_not_a_supported_candidate():
    fit=identification();fit['trials'][1]['scale']=1.8
    assert decide([round_trip(fit=fit)])['action']=='inconclusive'


def test_supported_estimate_outside_range_proposes_only_approved_partial_correction():
    result=decide([round_trip(fit=identification(2.4))])
    assert result['parameters']==dict(scale=2.,step_hz=100e6)
    assert 'partial correction' in result['reason']


def test_minimum_step_failure_is_inconclusive_and_never_outside_bounds():
    result=decide([round_trip(step=10e6,fit=identification(1.))])
    assert result['action']=='inconclusive'
    assert 'Minimum approved' in result['reason']


@pytest.mark.parametrize('budget',['rounds','elapsed'])
def test_original_host_budget_stops_proposals_and_keeps_best_measured_success(budget):
    rows=[round_trip(step=50e6,times=(2.,2.))]
    if budget=='rounds':rows*=12
    result=decide(rows,elapsed_s=1200 if budget=='elapsed' else 0)
    assert result['action']=='select' and result['parameters']['step_hz']==50e6
    failures=[round_trip(fit=identification())]*12
    assert decide(failures)['action']=='inconclusive'


@pytest.mark.parametrize('kw',[dict(max_roundtrips=13),dict(budget_s=1201),dict(initial_scale=2.1),dict(initial_scale=True),dict(goal_s=0)])
def test_no_budget_or_scale_expansion(kw):
    with pytest.raises(ValueError):decide([],**kw)


def test_fixed_profile_revision_mismatch_or_missing_prevents_ranking():
    row=round_trip(times=(1.,1.));row['criteria_revision']='relaxed'
    assert decide([row])['action']=='inconclusive'
    row.pop('criteria_revision')
    assert decide([row])['action']=='inconclusive'


def test_planning_preserves_raw_input_and_never_changes_settling_criteria():
    rows=[round_trip(fit=identification())];before=copy.deepcopy(rows)
    result=decide(rows)
    assert rows==before
    assert set(result['parameters'])=={'scale','step_hz'}
    assert result['criteria_revision']=='fixed'
