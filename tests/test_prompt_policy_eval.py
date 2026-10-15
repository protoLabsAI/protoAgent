"""Continuity comparison reuses shipped cases without silently dropping a lane."""
from langchain_core.messages import ToolMessage

from evals.prompt_policy import check_output, continuity_cases


def test_all_five_policy_questions_are_present():
    assert {c['id'] for c in continuity_cases()} == {
        'continuity_direct_recall', 'continuity_vague_recall', 'continuity_update',
        'continuity_unrelated_noise', 'continuity_abstention',
    }
    assert all(all('session_seed' in step for step in c.get('setup', [])) for c in continuity_cases())


def test_quality_check_detects_unrelated_leaks_and_forbidden_recall():
    case = next(c for c in continuity_cases() if c['id'] == 'continuity_unrelated_noise')
    assert check_output(case, 'Indexes accelerate reads but cost storage and slow writes.', [])
    assert not check_output(case, 'Your sourdough starter needs feeding.', [])
    assert not check_output(case, 'Indexes accelerate reads.', [ToolMessage(
        content='fixture', name='session_search', tool_call_id='t')])


def test_quality_check_requires_the_recorded_fact():
    case = next(c for c in continuity_cases() if c['id'] == 'continuity_direct_recall')
    assert check_output(case, 'Previously we chose 90 days.', [])
    assert not check_output(case, 'Previously we chose 30 days.', [])


def test_instance_commitments_survive_switching_sessions(monkeypatch):
    from types import SimpleNamespace
    from graph.projection import working_state_block
    from runtime.state import STATE

    items = [dict(id='global-task', status='open', priority=1, title='global commitment'),
             dict(id='project-a', status='open', priority=1, title='project A', session_id='a'),
             dict(id='project-b', status='open', priority=1, title='project B', session_id='b')]
    monkeypatch.setattr(STATE, 'tasks_store', SimpleNamespace(list=lambda **kw: items))
    for attr in ('goal_controller', 'watch_controller', 'scheduler'):
        monkeypatch.setattr(STATE, attr, None)
    monkeypatch.setattr('graph.work_providers.collect_work_sections', lambda cap: [])
    for sid in ('a', 'b'):
        text = working_state_block({'session_id': sid})
        assert all(item['id'] in text for item in items)
        assert text.count('← this goal') == 1
