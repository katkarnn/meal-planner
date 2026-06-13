"""
THE HARNESS ORCHESTRATOR
========================
This is the gap between PROPOSAL and APPLICATION. The worker proposes; the
harness decides. Responsibilities:

  - feed material in / take proposals out via the Workspace      (Pillar 3)
  - run the checkpoint pipeline, persisting each result          (Pillar 2)
  - convert violations into structured alarms                    (Pillar 4)
  - loop: hand checkpoint feedback back to the worker so its
    next proposal changes                                        (Must #2)
  - STOP and ASK a human when a failure is not auto-recoverable  (Should #3)
  - replay a run from any checkpoint forward                     (Should #2)

The four pillars are imported, not reimplemented here — the orchestrator only
sequences them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .alarms import Alarm, AlarmSink, AlarmType, Severity
from .checkpoints import PIPELINE, Checkpoint, CheckpointResult, Status
from .material import MealPlan, PlanRequest, Workspace, plan_from_dict


class Outcome(str, Enum):
    SHIPPED = "SHIPPED"
    BLOCKED_PENDING_HUMAN = "BLOCKED_PENDING_HUMAN"
    BLOCKED_HUMAN_REJECTED = "BLOCKED_HUMAN_REJECTED"
    BLOCKED_LOOP_EXHAUSTED = "BLOCKED_LOOP_EXHAUSTED"


@dataclass
class EscalationPacket:
    """What the harness hands a human when it refuses to guess."""
    reason: str
    blocking_alarms: list[dict]
    checkpoint_id: str
    attempt: int
    plan_digest: str
    question: str


# A human gate returns: True = approve override / ship anyway,
# False = reject, None = no human available right now -> halt pending review.
HumanGate = Callable[[EscalationPacket], Optional[bool]]


def default_gate(_: EscalationPacket) -> Optional[bool]:
    """Safe default: nobody is wired in, so HALT and wait for a human."""
    return None


@dataclass
class RunReport:
    outcome: Outcome
    worker: str
    attempts: int
    run_id: str
    checkpoints: list[dict] = field(default_factory=list)
    alarms: list[dict] = field(default_factory=list)
    escalation: Optional[dict] = None
    final_plan: Optional[dict] = None


class Harness:
    def __init__(self, workspace_root: str | Path = "runs",
                 max_attempts: int = 3, human_gate: HumanGate = default_gate):
        self.workspace_root = workspace_root
        self.max_attempts = max_attempts
        self.human_gate = human_gate

    # ----------------------------------------------------------------
    def run(self, worker, request_path: str | Path) -> RunReport:
        sink = AlarmSink()
        ws = Workspace(self.workspace_root)
        req = ws.load_request(request_path)

        feedback_lines: set[str] = set()   # cumulative: the harness remembers every correction
        last_results: list[CheckpointResult] = []
        last_plan: Optional[MealPlan] = None

        for attempt in range(1, self.max_attempts + 1):
            feedback = "\n".join(sorted(feedback_lines))
            try:
                proposal_dict = worker.propose(req, feedback)
                plan = plan_from_dict(proposal_dict, worker.name, attempt)
            except Exception as exc:   # malformed JSON, schema gap, API error...
                sink.raise_alarm(Alarm(
                    type=AlarmType.MALFORMED_PROPOSAL, severity=Severity.ERROR,
                    context={"worker": worker.name, "attempt": attempt, "error": str(exc)[:300]},
                    recommended_action="Worker output was unusable; retried with a schema reminder.",
                    source="orchestrator",
                ))
                feedback_lines.add(
                    "Return ONLY valid JSON matching the schema; the previous output could not be parsed.")
                continue
            last_plan = plan

            results = self._run_pipeline(plan, req, sink, ws)
            last_results = results

            # Did any stage demand a human? (non-auto-recoverable failure)
            human_stage = next((r for r in results if r.needs_human), None)
            if human_stage is not None:
                report = self._escalate(worker, attempt, plan, human_stage, sink, ws, req)
                if report is not None:
                    return report   # human rejected or no human -> stop
                # if human approved override, fall through to ship

            failed = [r for r in results if not r.passed]
            if not failed:
                return self._ship(worker, attempt, plan, results, sink, ws)

            # recoverable failures -> accumulate feedback and let the worker retry
            for r in failed:
                if r.feedback:
                    feedback_lines.add(r.feedback)

        # exhausted the attempt budget
        sink.raise_alarm(Alarm(
            type=AlarmType.WORKER_LOOP_EXHAUSTED, severity=Severity.ERROR,
            context={"attempts": self.max_attempts, "worker": worker.name},
            recommended_action="Escalate to a human planner; constraints may be unsatisfiable.",
            source="orchestrator",
        ))
        packet = self._packet("Worker could not satisfy checkpoints within the attempt budget.",
                              sink, last_results, self.max_attempts, last_plan)
        decision = self.human_gate(packet)
        outcome = (Outcome.BLOCKED_HUMAN_REJECTED if decision is False
                   else Outcome.BLOCKED_PENDING_HUMAN if decision is None
                   else Outcome.SHIPPED)
        return RunReport(outcome, worker.name, self.max_attempts, ws.run_id,
                         [r.to_dict() for r in last_results],
                         [a.to_dict() for a in sink.alarms],
                         escalation=packet.__dict__,
                         final_plan=None if outcome != Outcome.SHIPPED else _plan_dict(last_plan))

    # ----------------------------------------------------------------
    def _run_pipeline(self, plan, req, sink, ws) -> list[CheckpointResult]:
        """Run checkpoints IN ORDER. A CRITICAL failure stops the line so no
        downstream stage can wave an unsafe plan through. Each result is
        persisted for replay."""
        results = []
        for cp in PIPELINE:
            res = cp.evaluate(plan, req, sink)
            ws.snapshot(cp.id, plan, res.to_dict())
            results.append(res)
            critical = any(a.severity == Severity.CRITICAL for a in res.alarms)
            if not res.passed and critical:
                break   # do not evaluate downstream stages on an unsafe plan
        return results

    def _escalate(self, worker, attempt, plan, stage, sink, ws, req) -> Optional[RunReport]:
        sink.raise_alarm(Alarm(
            type=AlarmType.HUMAN_ESCALATION_REQUIRED, severity=Severity.CRITICAL,
            context={"checkpoint": stage.checkpoint_id, "reason": "non-auto-recoverable failure"},
            recommended_action="Human must review before any plan ships.",
            source="orchestrator",
        ))
        packet = self._packet(
            f"{stage.checkpoint_id} raised a non-auto-recoverable failure.",
            sink, [stage], attempt, plan)
        decision = self.human_gate(packet)
        if decision is True:
            return None   # human approves override -> caller proceeds to ship
        outcome = Outcome.BLOCKED_HUMAN_REJECTED if decision is False else Outcome.BLOCKED_PENDING_HUMAN
        return RunReport(outcome, worker.name, attempt, ws.run_id,
                         [stage.to_dict()], [a.to_dict() for a in sink.alarms],
                         escalation=packet.__dict__, final_plan=None)

    def _ship(self, worker, attempt, plan, results, sink, ws) -> RunReport:
        return RunReport(Outcome.SHIPPED, worker.name, attempt, ws.run_id,
                         [r.to_dict() for r in results],
                         [a.to_dict() for a in sink.alarms],
                         escalation=None, final_plan=_plan_dict(plan))

    def _packet(self, reason, sink, results, attempt, plan) -> EscalationPacket:
        blocking = [a.to_dict() for a in sink.blocking()]
        cp = results[-1].checkpoint_id if results else "n/a"
        return EscalationPacket(
            reason=reason, blocking_alarms=blocking, checkpoint_id=cp,
            attempt=attempt, plan_digest=(plan.digest if plan else "n/a"),
            question="Approve override and ship anyway, or reject?",
        )

    # ----------------------------------------------------------------
    def replay_from(self, run_id: str, from_checkpoint: str) -> list[dict]:
        """Replay a persisted run from `from_checkpoint` forward WITHOUT
        re-invoking the worker. Loads the persisted plan at that checkpoint and
        re-runs the remaining checkpoints (Should #2)."""
        ws = Workspace(self.workspace_root, run_id=run_id)
        plan, _ = ws.restore(from_checkpoint)
        req = PlanRequest.from_raw(_read_request(ws))
        sink = AlarmSink(echo=False)
        ids = [cp.id for cp in PIPELINE]
        start = ids.index(from_checkpoint)
        out = []
        for cp in PIPELINE[start:]:
            res = cp.evaluate(plan, req, sink)
            out.append(res.to_dict())
        return out


def _plan_dict(plan: MealPlan) -> dict:
    from .material import plan_to_dict
    return plan_to_dict(plan)


def _read_request(ws: Workspace) -> dict:
    import json
    return json.loads((ws.root / "request.json").read_text())
