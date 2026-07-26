"""Semantic persona-drift tier — the opt-in LLM judge (#2272)."""

from __future__ import annotations

from graph import soul_judge


def _judge(monkeypatch, raw: str):
    monkeypatch.setattr(soul_judge, "_invoke_judge", lambda prompt, model: raw)
    return soul_judge.judge_soul_drift("baseline persona", "current persona")


def test_doctrine_leak_with_identity_intact_is_the_case_the_tier_exists_for(monkeypatch):
    """Retention can't tell 'rewritten' from 'procedure accreted' — both read as a low
    ratio. This is the second one, and the whole reason a judge is needed."""
    verdict = _judge(
        monkeypatch,
        '{"drift_score": 0.4, "identity_preserved": true, "doctrine_leak": true,'
        ' "rationale": "Same role and voice, but three runbook sections were added."}',
    )

    assert verdict["identity_preserved"] is True
    assert verdict["doctrine_leak"] is True
    assert verdict["drift_score"] == 0.4


def test_identity_loss_is_reported_separately_from_leak(monkeypatch):
    verdict = _judge(
        monkeypatch,
        '{"drift_score": 0.9, "identity_preserved": false, "doctrine_leak": false, "rationale": "Different agent."}',
    )

    assert verdict["identity_preserved"] is False and verdict["doctrine_leak"] is False


def test_a_fenced_or_chatty_reply_still_parses(monkeypatch):
    verdict = _judge(
        monkeypatch,
        'Sure!\n```json\n{"drift_score": 0.1, "identity_preserved": true, '
        '"doctrine_leak": false, "rationale": "Minor rewording."}\n```',
    )

    assert verdict["drift_score"] == 0.1


def test_an_out_of_range_score_is_clamped(monkeypatch):
    """A judge answering 1.5 must not poison a threshold comparison."""
    hi = _judge(monkeypatch, '{"drift_score": 1.5, "identity_preserved": true, "doctrine_leak": false}')
    lo = _judge(monkeypatch, '{"drift_score": -0.3, "identity_preserved": true, "doctrine_leak": false}')

    assert hi["drift_score"] == 1.0 and lo["drift_score"] == 0.0


def test_a_missing_identity_key_defaults_to_preserved(monkeypatch):
    """Absence of a verdict isn't evidence the persona was replaced — and a false
    'identity lost' alarm is the expensive one."""
    verdict = _judge(monkeypatch, '{"drift_score": 0.2, "doctrine_leak": false}')

    assert verdict["identity_preserved"] is True


def test_a_non_json_reply_yields_no_verdict(monkeypatch):
    """None means 'no verdict', which must stay distinct from 'no drift'."""
    assert _judge(monkeypatch, "I think it's fine, honestly.") is None


def test_bad_json_yields_no_verdict(monkeypatch):
    assert _judge(monkeypatch, '{"drift_score": }') is None


def test_a_judge_exception_never_propagates(monkeypatch):
    def _boom(prompt, model):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(soul_judge, "_invoke_judge", _boom)

    assert soul_judge.judge_soul_drift("a", "b") is None


def test_empty_personas_short_circuit_without_calling_the_model(monkeypatch):
    called = []
    monkeypatch.setattr(soul_judge, "_invoke_judge", lambda p, m: called.append(1) or "{}")

    assert soul_judge.judge_soul_drift("", "current") is None
    assert soul_judge.judge_soul_drift("baseline", "   ") is None
    assert called == []


def test_a_long_persona_is_clipped_so_the_prompt_stays_bounded():
    prompt = soul_judge._build_prompt("x" * 50000, "y")

    assert "truncated" in prompt and len(prompt) < 30000


# ── the tier gate ─────────────────────────────────────────────────────────────
def test_tier_is_off_by_default(tmp_path):
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig.from_dict({})

    assert cfg.soul_drift_judge_enabled is False and cfg.soul_drift_judge_model == ""


def test_tier_knobs_parse():
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig.from_dict(
        {"soul": {"drift": {"judge": {"enabled": True, "model": "protolabs/reasoning"}}}}
    )

    assert cfg.soul_drift_judge_enabled is True
    assert cfg.soul_drift_judge_model == "protolabs/reasoning"


def test_disabled_tier_never_calls_the_judge(monkeypatch):
    from server import agent_init

    class _Cfg:
        soul_drift_judge_enabled = False

    monkeypatch.setattr(soul_judge, "_invoke_judge", lambda p, m: (_ for _ in ()).throw(AssertionError))

    assert agent_init._judge_soul_drift(_Cfg(), {"baseline_id": "v1"}) is None
