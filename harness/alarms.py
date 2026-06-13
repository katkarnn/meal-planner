"""
PILLAR 4 — ALARMS
=================
Alarms are *structured*, not log lines. Every alarm has a named type, a
severity, a context payload, and a recommended action. Anything in the harness
can raise one; the sink collects them and the orchestrator decides whether an
alarm halts the run or escalates to a human.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from enum import Enum


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"   # safety-critical: must block shipping


class AlarmType(str, Enum):
    # safety-critical
    ALLERGEN_DETECTED = "ALLERGEN_DETECTED"
    CALORIE_FLOOR_BREACH = "CALORIE_FLOOR_BREACH"
    CALORIE_CEILING_BREACH = "CALORIE_CEILING_BREACH"
    # correctness / completeness
    UNCONFIRMED_INGREDIENT = "UNCONFIRMED_INGREDIENT"
    DIET_VIOLATION = "DIET_VIOLATION"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MALFORMED_PROPOSAL = "MALFORMED_PROPOSAL"
    # process
    WORKER_LOOP_EXHAUSTED = "WORKER_LOOP_EXHAUSTED"
    HUMAN_ESCALATION_REQUIRED = "HUMAN_ESCALATION_REQUIRED"


@dataclass
class Alarm:
    type: AlarmType
    severity: Severity
    context: dict                 # structured details (meal, ingredient, day, kcal, ...)
    recommended_action: str
    source: str                   # which guardrail/checkpoint raised it
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["severity"] = self.severity.value
        return d

    def is_blocking(self) -> bool:
        return self.severity == Severity.CRITICAL


class AlarmSink:
    """Collects alarms for a run and emits them as structured JSONL to stderr
    so they're greppable and machine-readable in production."""

    def __init__(self, echo: bool = True):
        self.alarms: list[Alarm] = []
        self.echo = echo

    def raise_alarm(self, alarm: Alarm) -> Alarm:
        self.alarms.append(alarm)
        if self.echo:
            sys.stderr.write("ALARM " + json.dumps(alarm.to_dict()) + "\n")
        return alarm

    def blocking(self) -> list[Alarm]:
        return [a for a in self.alarms if a.is_blocking()]

    def by_type(self, t: AlarmType) -> list[Alarm]:
        return [a for a in self.alarms if a.type == t]
