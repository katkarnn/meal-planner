"""
PILLAR 3 — MATERIAL HANDLING
============================
Clean, typed interfaces for passing material *into* the agent and getting a
proposal *out* of it. The agent receives a `PlanRequest` and returns a
`MealPlan` proposal. It never reads disk, never parses raw user input, and
never decides what ships. Everything else — normalization, versioning,
snapshots — is the harness's job.

This decoupling is what makes the agent interface swappable (Should #1): a
worker only has to speak `PlanRequest -> MealPlan`.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Material that flows IN to the agent
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanRequest:
    """Normalized, validated description of what the user wants. This is the
    ONLY thing a worker is handed; it is immutable and identical across every
    worker the harness drives."""
    user_id: str
    days: int
    allergens: tuple[str, ...]          # lowercase ingredient terms the user reacts to
    diet_type: str                       # e.g. "pescatarian", "vegetarian", "omnivore"
    goal: str                            # e.g. "maintain", "gain", "recomp"
    daily_calorie_target: int            # user-stated target (validated against safety floor)
    budget_cents: int                    # weekly budget ceiling
    pantry: tuple[str, ...]              # ingredients CONFIRMED available
    household_size: int = 1

    @staticmethod
    def from_raw(raw: dict) -> "PlanRequest":
        return PlanRequest(
            user_id=raw["user_id"],
            days=raw["days"],
            allergens=tuple(a.lower().strip() for a in raw["allergens"]),
            diet_type=raw["diet_type"].lower(),
            goal=raw["goal"],
            daily_calorie_target=int(raw["daily_calorie_target"]),
            budget_cents=int(raw["budget_cents"]),
            pantry=tuple(p.lower().strip() for p in raw.get("pantry", [])),
            household_size=int(raw.get("household_size", 1)),
        )


# --------------------------------------------------------------------------
# Material that flows OUT of the agent (the PROPOSAL — not yet shipped)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ingredient:
    name: str
    qty: str          # free text e.g. "200g", "1 cup"

    @property
    def key(self) -> str:
        return self.name.lower().strip()


@dataclass(frozen=True)
class Meal:
    day: int
    slot: str                       # breakfast | lunch | dinner
    name: str
    ingredients: tuple[Ingredient, ...]
    calories: int
    cost_cents: int


@dataclass
class MealPlan:
    """A worker's PROPOSAL. The harness — never the worker — decides if this
    ever reaches the user."""
    request_user: str
    meals: tuple[Meal, ...]
    produced_by: str
    attempt: int = 1
    notes: str = ""

    @property
    def digest(self) -> str:
        blob = "|".join(f"{m.day}:{m.slot}:{m.name}:{m.calories}" for m in self.meals)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def calories_by_day(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for m in self.meals:
            out[m.day] = out.get(m.day, 0) + m.calories
        return out

    def total_cost_cents(self) -> int:
        return sum(m.cost_cents for m in self.meals)

    def all_ingredients(self) -> list[tuple[Meal, Ingredient]]:
        return [(m, ing) for m in self.meals for ing in m.ingredients]


# --------------------------------------------------------------------------
# Serialization helpers (workers emit dicts; harness builds typed objects)
# --------------------------------------------------------------------------
def plan_from_dict(d: dict, produced_by: str, attempt: int) -> MealPlan:
    meals = tuple(
        Meal(
            day=m["day"], slot=m["slot"], name=m["name"],
            calories=int(m["calories"]), cost_cents=int(m["cost_cents"]),
            ingredients=tuple(Ingredient(i["name"], i.get("qty", "")) for i in m["ingredients"]),
        )
        for m in d["meals"]
    )
    return MealPlan(request_user=d.get("request_user", ""), meals=meals,
                    produced_by=produced_by, attempt=attempt, notes=d.get("notes", ""))


def plan_to_dict(plan: MealPlan) -> dict:
    return {
        "request_user": plan.request_user,
        "produced_by": plan.produced_by,
        "attempt": plan.attempt,
        "notes": plan.notes,
        "meals": [
            {
                "day": m.day, "slot": m.slot, "name": m.name,
                "calories": m.calories, "cost_cents": m.cost_cents,
                "ingredients": [{"name": i.name, "qty": i.qty} for i in m.ingredients],
            }
            for m in plan.meals
        ],
    }


# --------------------------------------------------------------------------
# The Workspace — material bus + persistence layer (enables replay)
# --------------------------------------------------------------------------
class Workspace:
    """Owns one run's material on disk. Every checkpoint snapshot lands here,
    which is what makes replay-from-checkpoint possible (Should #2)."""

    def __init__(self, root: str | Path, run_id: str | None = None):
        self.run_id = run_id or f"run-{int(time.time() * 1000)}"
        self.root = Path(root) / self.run_id
        (self.root / "checkpoints").mkdir(parents=True, exist_ok=True)

    def load_request(self, raw_path: str | Path) -> PlanRequest:
        raw = json.loads(Path(raw_path).read_text())
        (self.root / "request.json").write_text(json.dumps(raw, indent=2))
        return PlanRequest.from_raw(raw)

    # ---- checkpoint snapshots ------------------------------------------
    def snapshot(self, checkpoint_id: str, plan: MealPlan, result: dict) -> None:
        record = {
            "checkpoint_id": checkpoint_id,
            "ts": time.time(),
            "plan": plan_to_dict(plan),
            "result": result,
        }
        (self.root / "checkpoints" / f"{checkpoint_id}.json").write_text(
            json.dumps(record, indent=2)
        )

    def has_snapshot(self, checkpoint_id: str) -> bool:
        return (self.root / "checkpoints" / f"{checkpoint_id}.json").exists()

    def restore(self, checkpoint_id: str) -> tuple[MealPlan, dict]:
        record = json.loads(
            (self.root / "checkpoints" / f"{checkpoint_id}.json").read_text()
        )
        plan = plan_from_dict(
            record["plan"], record["plan"]["produced_by"], record["plan"]["attempt"]
        )
        return plan, record["result"]

    def completed_checkpoints(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "checkpoints").glob("*.json"))
