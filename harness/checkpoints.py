"""
PILLAR 2 — CHECKPOINTS
======================
Checkpoints are ordered gates with EXPLICIT pass/fail criteria (Must #3). Each
one inspects the current proposal and returns PASS or FAIL plus the evidence it
judged on. Checkpoints translate guardrail results into alarms and gate
decisions. Their results are persisted by the Workspace, so a run can be
replayed from any checkpoint forward (Should #2).

Order matters: SAFETY runs before FIT runs before COMPLETENESS. A CRITICAL
failure at SAFETY stops the line — nothing downstream gets a chance to ship an
unsafe plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import guardrails as G
from .alarms import Alarm, AlarmSink, AlarmType, Severity
from .material import MealPlan, PlanRequest


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class CheckpointResult:
    checkpoint_id: str
    status: Status
    criteria: str                      # the explicit pass/fail rule
    evidence: dict = field(default_factory=dict)
    alarms: list[Alarm] = field(default_factory=list)
    needs_human: bool = False
    feedback: str = ""                 # forwarded to the worker on a recoverable fail

    @property
    def passed(self) -> bool:
        return self.status == Status.PASS

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "status": self.status.value,
            "criteria": self.criteria,
            "evidence": self.evidence,
            "alarms": [a.to_dict() for a in self.alarms],
            "needs_human": self.needs_human,
            "feedback": self.feedback,
        }


@dataclass
class Checkpoint:
    id: str
    criteria: str
    evaluate: Callable[[MealPlan, PlanRequest, AlarmSink], CheckpointResult]


# --------------------------------------------------------------------------
# Helper: run a named subset of guardrails and fold them into a checkpoint
# --------------------------------------------------------------------------
def _run_guardrails(cp_id: str, criteria: str, ids: list[str],
                    plan: MealPlan, req: PlanRequest, sink: AlarmSink) -> CheckpointResult:
    selected = [g for g in G.REGISTRY if g.id in ids]
    results = [g.check(plan, req) for g in selected]
    failed = [r for r in results if not r.ok]

    alarms = []
    for r in failed:
        if r.alarm_type:
            alarms.append(sink.raise_alarm(Alarm(
                type=r.alarm_type, severity=r.severity,
                context={"guardrail": r.guardrail_id, "violations": r.violations},
                recommended_action=r.feedback or "Re-propose to satisfy the guardrail.",
                source=f"checkpoint:{cp_id}",
            )))

    needs_human = any((not r.ok) and (not r.auto_recoverable) for r in results)
    status = Status.PASS if not failed else Status.FAIL
    return CheckpointResult(
        checkpoint_id=cp_id, status=status, criteria=criteria,
        evidence={"guardrails": [{"id": r.guardrail_id, "ok": r.ok,
                                  "violations": r.violations} for r in results]},
        alarms=alarms, needs_human=needs_human,
        feedback=G.build_feedback(results),
    )


# --------------------------------------------------------------------------
# CP1 — SCHEMA: the proposal is structurally well-formed
# --------------------------------------------------------------------------
def cp_schema(plan: MealPlan, req: PlanRequest, sink: AlarmSink) -> CheckpointResult:
    problems = []
    days_present = {m.day for m in plan.meals}
    for d in range(1, req.days + 1):
        if d not in days_present:
            problems.append(f"day {d} has no meals")
    for m in plan.meals:
        if not m.ingredients:
            problems.append(f"{m.name} has no ingredients")
        if m.calories <= 0:
            problems.append(f"{m.name} has non-positive calories")
    if problems:
        alarm = sink.raise_alarm(Alarm(
            type=AlarmType.MALFORMED_PROPOSAL, severity=Severity.ERROR,
            context={"problems": problems}, source="checkpoint:CP1_SCHEMA",
            recommended_action="Re-propose a complete plan covering every requested day.",
        ))
        return CheckpointResult("CP1_SCHEMA", Status.FAIL,
                                "Every requested day has >=1 meal; every meal has ingredients and calories>0.",
                                evidence={"problems": problems}, alarms=[alarm],
                                feedback="Fix structure: " + "; ".join(problems))
    return CheckpointResult("CP1_SCHEMA", Status.PASS,
                            "Every requested day has >=1 meal; every meal has ingredients and calories>0.",
                            evidence={"days": sorted(days_present), "meal_count": len(plan.meals)})


# --------------------------------------------------------------------------
# CP2 — SAFETY: the safety-critical gate. Zero CRITICAL violations to pass.
# --------------------------------------------------------------------------
def cp_safety(plan: MealPlan, req: PlanRequest, sink: AlarmSink) -> CheckpointResult:
    return _run_guardrails(
        "CP2_SAFETY",
        "Zero CRITICAL violations: no allergens present AND no calorie-floor breach.",
        ["ALLERGEN_EXCLUSION", "CALORIE_FLOOR", "CALORIE_CEILING"],
        plan, req, sink,
    )


# --------------------------------------------------------------------------
# CP3 — FIT: diet + budget compliance
# --------------------------------------------------------------------------
def cp_fit(plan: MealPlan, req: PlanRequest, sink: AlarmSink) -> CheckpointResult:
    return _run_guardrails(
        "CP3_FIT",
        "Diet type respected AND total cost within budget.",
        ["DIET_COMPLIANCE", "BUDGET_LIMIT"],
        plan, req, sink,
    )


# --------------------------------------------------------------------------
# CP4 — COMPLETENESS: every ingredient confirmed or on the shopping list
# --------------------------------------------------------------------------
def cp_completeness(plan: MealPlan, req: PlanRequest, sink: AlarmSink) -> CheckpointResult:
    return _run_guardrails(
        "CP4_COMPLETENESS",
        "No unconfirmed ingredients: each is pantry-stocked or flagged 'buy:'.",
        ["INGREDIENT_CONFIRMED"],
        plan, req, sink,
    )


# Ordered pipeline. SAFETY precedes everything that could ship a plan.
PIPELINE: list[Checkpoint] = [
    Checkpoint("CP1_SCHEMA", "well-formed proposal", cp_schema),
    Checkpoint("CP2_SAFETY", "no allergens, no calorie-floor breach", cp_safety),
    Checkpoint("CP3_FIT", "diet + budget", cp_fit),
    Checkpoint("CP4_COMPLETENESS", "ingredients confirmed / shopping list", cp_completeness),
]
