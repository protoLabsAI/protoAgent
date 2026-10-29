"""Trace attribution uses fake A2A/telemetry responses; no live server required."""

import json
from dataclasses import asdict

import httpx
import pytest

from evals.client import AgentClient
from evals.runner import CaseResult, run_one


@pytest.mark.asyncio
@pytest.mark.parametrize('direct,telemetry,expected', [
    ('a2a-trace', 'stored-trace', 'a2a-trace'),
    ('', 'stored-trace', 'stored-trace'),
    ('', '', ''),
])
async def test_case_trace_sources(monkeypatch, direct, telemetry, expected):
    contexts = []
    reads = []

    def respond(request):
        if request.url.path == '/a2a':
            message = json.loads(request.content)['params']['message']
            contexts.append(message['contextId'])
            return httpx.Response(200, json={'result': {'task': {
                'id': 'task', 'status': {'state': 'TASK_STATE_COMPLETED'},
                'metadata': {'trace_id': direct},
            }}})
        assert request.url.path == '/api/telemetry/recent'
        assert request.headers['Authorization'] == 'Bearer test-token'
        reads.append(request)
        return httpx.Response(200, json={'turns': [
            {'session_id': 'unrelated', 'trace_id': 'wrong', 'ended_at': '9999'},
            {'session_id': contexts[-1], 'trace_id': telemetry, 'ended_at': '9999'},
        ]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    client = AgentClient('http://eval.test', bearer='test-token')
    case = {'id': 'example', 'category': 'test', 'name': 'example', 'prompt': 'hello'}
    result = await run_one(client, case)
    assert result.passed
    assert asdict(result)['trace_id'] == expected
    assert bool(reads) == (not direct)
    await run_one(client, case)
    assert contexts[0] != contexts[1]
    assert 'context_id' not in case


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['missing', 'stale', 'unauthorized', 'invalid', 'offline'])
async def test_unavailable_telemetry_is_empty(monkeypatch, response):
    def respond(request):
        if response == 'offline':
            raise httpx.ConnectError('offline', request=request)
        if response == 'unauthorized':
            return httpx.Response(401)
        if response == 'invalid':
            return httpx.Response(200, text='not JSON')
        return httpx.Response(200, json={'turns': [] if response == 'missing' else [
            {'session_id': 'ctx', 'trace_id': 'old', 'ended_at': '2000-01-01'}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    assert await AgentClient('http://eval.test').trace_for_context('ctx', since='2026-01-01') == ''


def test_non_turn_and_skipped_report_rows_have_empty_trace():
    for kind in ('agent_card', 'auth_check', 'skipped'):
        assert asdict(CaseResult(kind, 'test', kind, True, 'OK'))['trace_id'] == ''


@pytest.mark.asyncio
async def test_stream_trace_is_preserved(monkeypatch):
    frames = [
        {'task': {'id': 'task', 'metadata': {'trace_id': 'stream-trace'}}},
        {'statusUpdate': {'taskId': 'task', 'status': {'state': 'TASK_STATE_COMPLETED'}, 'final': True}},
    ]
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'content-type': 'text/event-stream'},
        text=''.join('data: ' + json.dumps({'result': frame}) + '\n\n' for frame in frames)))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    result = await run_one(AgentClient('http://eval.test'), {
        'id': 'stream', 'category': 'test', 'name': 'stream', 'kind': 'stream', 'prompt': 'hello',
    })
    assert result.passed
    assert result.trace_id == 'stream-trace'


@pytest.mark.asyncio
async def test_failed_turn_still_has_trace(monkeypatch):
    from evals.client import TaskResult

    client = AgentClient('http://eval.test')

    async def ask(*args, **kwargs):
        return TaskResult('task', 'failed', trace_id='failed-trace')

    monkeypatch.setattr(client, 'ask', ask)
    result = await run_one(client, {'id': 'fail', 'category': 'test', 'name': 'fail', 'prompt': 'hello'})
    assert not result.passed
    assert result.trace_id == 'failed-trace'


@pytest.mark.asyncio
async def test_operator_goal_does_not_kick_and_attributes_trigger(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'DELETE':
            return httpx.Response(200, json={'cleared': True})
        if request.url.path == '/api/goals':
            body = json.loads(request.content)
            assert body['kick'] is False
            assert body['verifier'] == {'type': 'command', 'command': 'true'}
            assert body['session_id'] == 'pinned'
            return httpx.Response(200, json={'ok': True})
        if request.url.path == '/a2a':
            message = json.loads(request.content)['params']['message']
            assert message['contextId'] == 'pinned'
            assert message['parts'][0]['text'] == 'make progress'
            return httpx.Response(200, json={'result': {'task': {
                'id': 'trigger', 'status': {'state': 'TASK_STATE_COMPLETED'},
                'metadata': {'trace_id': 'trigger-trace'},
            }}})
        assert request.url.path == '/api/goals/pinned'
        return httpx.Response(200, json={'goal': {'status': 'achieved'}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    result = await run_one(AgentClient('http://eval.test'), {
        'id': 'goal', 'category': 'goal', 'name': 'goal', 'kind': 'goal',
        'context_id': 'pinned', 'goal_setup': 'operator',
        'set_goal': {'condition': 'ready', 'verifier': {'type': 'command', 'command': 'true'}},
        'prompt': 'make progress', 'expected_goal_status': 'achieved',
    })
    assert result.passed
    assert result.trace_id == 'trigger-trace'
    assert requests[-1].method == 'DELETE'


def test_grader_extracts_structured_text(monkeypatch):
    from types import SimpleNamespace
    from evals import judge
    from graph import llm
    from graph.config import LangGraphConfig
    from langchain_core.messages import AIMessage

    monkeypatch.setattr(LangGraphConfig, 'from_yaml', lambda path: SimpleNamespace(model_name='fake'))
    monkeypatch.setattr(llm, 'create_llm', lambda *args, **kwargs: SimpleNamespace(
        invoke=lambda messages: AIMessage(content=[{
            'type': 'text', 'text': '{"criteria": [{"met": true, "why": "present"}]}'
        }])))
    verdict = judge.score_rubric('answer', ['criterion'])
    assert verdict.error is None
    assert verdict.score == 1.0


def test_report_serializes_trace_for_every_row(tmp_path):
    from evals.runner import _save_report

    path = tmp_path / 'run.json'
    _save_report([
        CaseResult('turn', 'test', 'turn', True, 'OK', trace_id='trace'),
        CaseResult('card', 'test', 'card', True, 'OK'),
        CaseResult('auth', 'test', 'auth', True, 'OK'),
        CaseResult('skipped', 'test', 'skipped', True, 'prerequisite', skipped=True),
        CaseResult('failed', 'test', 'failed', False, 'error'),
    ], path)
    assert [row['trace_id'] for row in json.loads(path.read_text())['results']] == ['trace', '', '', '', '']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['agent_card', 'auth_check'])
async def test_non_turn_does_not_query_telemetry(monkeypatch, kind):
    def respond(request):
        assert '/telemetry/' not in request.url.path
        if request.method == 'GET':
            return httpx.Response(200, json={'name': 'test'})
        return httpx.Response(401)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    result = await run_one(AgentClient('http://eval.test'), {
        'id': kind, 'category': 'test', 'name': kind, 'kind': kind,
    })
    assert result.passed
    assert result.trace_id == ''


@pytest.mark.asyncio
async def test_goal_missing_trigger_trace_never_uses_setup_trace(monkeypatch):
    from datetime import UTC, datetime

    setup_row = {}

    def respond(request):
        if request.method == 'DELETE':
            return httpx.Response(200, json={})
        if request.url.path == '/api/telemetry/recent':
            return httpx.Response(200, json={'turns': [setup_row]})
        if request.url.path.startswith('/api/goals/'):
            return httpx.Response(200, json={'goal': {'status': 'achieved'}})
        message = json.loads(request.content)['params']['message']
        is_setup = message['parts'][0]['text'].startswith('/goal ')
        if is_setup:
            setup_row.update(session_id=message['contextId'], trace_id='setup-only',
                             ended_at=datetime.now(UTC).isoformat())
        return httpx.Response(200, json={'result': {'task': {
            'id': 'setup' if is_setup else 'trigger',
            'status': {'state': 'TASK_STATE_COMPLETED'},
            'artifacts': [{'parts': [{'text': 'Goal set' if is_setup else 'done'}]}],
            'metadata': {'trace_id': 'setup-only' if is_setup else ''},
        }}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    result = await run_one(AgentClient('http://eval.test'), {
        'id': 'goal', 'category': 'goal', 'name': 'goal', 'kind': 'goal',
        'set_goal': {'condition': 'ready'}, 'prompt': 'go', 'expected_goal_status': 'achieved',
    })
    assert result.passed
    assert result.trace_id == ''
