import pytest
from ldcon.jobs.study_execution import parse_proposal


def test_complete_single_final_decision_can_follow_acp_progress():
 text='Session is admitted (`state: idle`, `points: 0`). I will read the evidence first.{"action":"finish","parameters":{},"reason":"No improvement justified."}'
 assert parse_proposal(text,allow_progress_prefix=True)['action']=='finish'
 with pytest.raises(ValueError):parse_proposal(text)


@pytest.mark.parametrize('text',[
 'Earlier {} then {"action":"finish"}',
 'Progress [1] then {"action":"finish"}',
 'Progress {"action":"finish"} {"action":"begin"}',
 'Progress {"action":"finish"} disregard this',
 'Progress {"action":"finish"',
 'Progress {"value":NaN}',
 'No decision',
])
def test_ambiguous_partial_nonfinite_or_multiple_answers_rejected(text):
 with pytest.raises(ValueError):parse_proposal(text,allow_progress_prefix=True)
