"""End-to-end and unit tests for the safety harness."""
import json
import tempfile
from pathlib import Path

import pytest

from harness import (Harness, MockMealWorker, TemplateMealWorker,
                     PlanRequest, guardrails as G)
from harness.harness import Outcome
from harness.material import plan_from_dict


def _req(**over):
    base = {
        "user_id": "t", "days": 2, "allergens": ["peanut", "shellfish"],
        "diet_type": "pescatarian", "goal": "maintain", "daily_calorie_target": 2000,
        "budget_cents": 3000,
        "pantry": ["oats", "banana", "quinoa", "chickpeas", "olive oil",
                   "pasta", "tomato sauce", "sunflower seed butter"],
    }
    base.update(over)
    return PlanRequest.from_raw(base)


def _write(req_dict, tmp):
    p = Path(tmp) / "r.json"
    p.write_text(json.dumps(req_dict))
    return str(p)


# ----------------------------- guardrail units -----------------------------
def test_allergen_guardrail_catches_alias():
    req = _req()
    plan = plan_from_dict({"request_user": "t", "meals": [
        {"day": 1, "slot": "lunch", "name": "Shrimp bowl", "calories": 600, "cost_cents": 200,
         "ingredients": [{"name": "shrimp", "qty": "100g"}]}]}, "x", 1)
    res = G.check_allergens(plan, req)
    assert not res.ok and res.severity == G.Severity.CRITICAL
    assert res.violations[0]["allergen"] == "shellfish"


def test_calorie_floor_stated_target_is_not_auto_recoverable():
    req = _req(daily_calorie_target=900)
    plan = plan_from_dict({"request_user": "t", "meals": [
        {"day": 1, "slot": "x", "name": "m", "calories": 900, "cost_cents": 100,
         "ingredients": [{"name": "oats", "qty": "1"}]}]}, "x", 1)
    res = G.check_calorie_floor(plan, req)
    assert not res.ok and res.auto_recoverable is False


def test_diet_and_budget_guardrails():
    req = _req(diet_type="vegetarian", budget_cents=100)
    plan = plan_from_dict({"request_user": "t", "meals": [
        {"day": 1, "slot": "d", "name": "Beef", "calories": 700, "cost_cents": 999,
         "ingredients": [{"name": "beef", "qty": "1"}]}]}, "x", 1)
    assert not G.check_diet(plan, req).ok
    assert not G.check_budget(plan, req).ok


# ----------------------------- full-harness flows ---------------------------
def test_mock_worker_self_corrects_and_ships():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(_req_dict(), tmp)
        rep = Harness(workspace_root=tmp, max_attempts=4).run(MockMealWorker(), path)
        assert rep.outcome == Outcome.SHIPPED
        assert rep.attempts >= 2  # it had to change behavior at least once
        assert all(c["status"] == "PASS" for c in rep.checkpoints)


def test_unsafe_target_stops_for_human():
    with tempfile.TemporaryDirectory() as tmp:
        d = _req_dict(); d["daily_calorie_target"] = 900
        path = _write(d, tmp)
        # default gate => no human available => halt pending review
        rep = Harness(workspace_root=tmp, max_attempts=4).run(MockMealWorker(), path)
        assert rep.outcome == Outcome.BLOCKED_PENDING_HUMAN
        assert rep.escalation is not None
        assert any(a["type"] == "CALORIE_FLOOR_BREACH" for a in rep.alarms)


def test_human_can_reject():
    with tempfile.TemporaryDirectory() as tmp:
        d = _req_dict(); d["daily_calorie_target"] = 900
        path = _write(d, tmp)
        rep = Harness(workspace_root=tmp, max_attempts=4,
                      human_gate=lambda pkt: False).run(MockMealWorker(), path)
        assert rep.outcome == Outcome.BLOCKED_HUMAN_REJECTED


def test_template_worker_is_portable():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(_req_dict(), tmp)
        rep = Harness(workspace_root=tmp, max_attempts=4).run(TemplateMealWorker(), path)
        assert rep.outcome == Outcome.SHIPPED  # different worker, zero harness changes


def test_replay_is_deterministic_and_skips_worker():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(_req_dict(), tmp)
        h = Harness(workspace_root=tmp, max_attempts=4)
        rep = h.run(MockMealWorker(), path)
        replay = h.replay_from(rep.run_id, "CP3_FIT")
        ids = [r["checkpoint_id"] for r in replay]
        assert ids == ["CP3_FIT", "CP4_COMPLETENESS"]
        assert all(r["status"] == "PASS" for r in replay)


def test_malformed_worker_output_is_handled():
    class BrokenWorker:
        name = "broken"
        def propose(self, req, feedback):
            return {"oops": "no meals key"}
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(_req_dict(), tmp)
        rep = Harness(workspace_root=tmp, max_attempts=2).run(BrokenWorker(), path)
        assert any(a["type"] == "MALFORMED_PROPOSAL" for a in rep.alarms)
        assert rep.outcome != Outcome.SHIPPED


# ----------------------------- helpers --------------------------------------
def _req_dict():
    return {
        "user_id": "t", "days": 2, "allergens": ["peanut", "shellfish"],
        "diet_type": "pescatarian", "goal": "maintain", "daily_calorie_target": 2000,
        "budget_cents": 3000,
        "pantry": ["oats", "banana", "quinoa", "chickpeas", "olive oil",
                   "pasta", "tomato sauce", "sunflower seed butter"],
    }
