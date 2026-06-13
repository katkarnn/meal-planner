"""
Web API + static host for the Personal Meal-Planner Safety Harness.

The API is a thin shell around the harness. It validates input, selects a
worker, runs the harness (which alone decides ship/block/escalate), and returns
the structured RunReport. The harness — not this layer — owns all safety.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from harness import (Harness, MockMealWorker, TemplateMealWorker, ClaudeMealWorker,
                     GeminiMealWorker, guardrails as G)
from harness.harness import Outcome

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
RUNS_DIR = os.environ.get("HARNESS_RUNS_DIR", str(ROOT / "runs"))
MAX_ATTEMPTS = int(os.environ.get("HARNESS_MAX_ATTEMPTS", "4"))

app = FastAPI(title="Meal-Planner Safety Harness", version="1.0.0")


# ---------------------------------------------------------------- workers ----
def _module_installed(name: str) -> bool:
    """A worker we advertise as available must be RUNNABLE — i.e. its SDK is
    actually importable, not merely key-configured. Guards against a server
    that has the API key set but the SDK missing from its environment."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _module_installed("anthropic")


def _gemini_available() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY")) and _module_installed("google.genai")


def build_worker(name: str):
    name = (name or "mock").lower()
    if name == "mock":
        return MockMealWorker()
    if name == "template":
        return TemplateMealWorker()
    if name == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(
                status_code=400,
                detail="The live Claude worker needs ANTHROPIC_API_KEY set on the server. "
                       "Set it in Railway > Variables, or pick the mock/template worker.",
            )
        if not _module_installed("anthropic"):
            raise HTTPException(
                status_code=400,
                detail="The Claude SDK isn't installed on the server (pip install anthropic). "
                       "Pick the mock/template worker, or redeploy so requirements.txt installs.",
            )
        return ClaudeMealWorker(model=os.environ.get("HARNESS_CLAUDE_MODEL", "claude-sonnet-4-6"))
    if name == "gemini":
        if not os.environ.get("GOOGLE_API_KEY"):
            raise HTTPException(
                status_code=400,
                detail="The live Gemini worker needs GOOGLE_API_KEY set on the server. "
                       "Get a free key at aistudio.google.com, or pick the mock/template worker.",
            )
        if not _module_installed("google.genai"):
            raise HTTPException(
                status_code=400,
                detail="The Gemini SDK isn't installed on the server (pip install google-genai). "
                       "Pick the mock/template worker, or redeploy so requirements.txt installs.",
            )
        return GeminiMealWorker(model=os.environ.get("HARNESS_GEMINI_MODEL", "gemini-2.5-flash"))
    raise HTTPException(status_code=400,
                        detail=f"Unknown worker '{name}'. Use mock | template | claude | gemini.")


# ---------------------------------------------------------------- schemas ----
class PlanRequestIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=120)
    days: int = Field(ge=1, le=14)
    allergens: list[str] = Field(default_factory=list)
    diet_type: str = Field(min_length=1, max_length=40)
    goal: str = Field(min_length=1, max_length=40)
    daily_calorie_target: int = Field(ge=1, le=20000)
    budget_cents: int = Field(ge=1, le=10_000_00)
    pantry: list[str] = Field(default_factory=list)
    household_size: int = Field(default=1, ge=1, le=20)

    @field_validator("allergens", "pantry")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]

    def to_raw(self) -> dict:
        return self.model_dump()


class PlanBody(BaseModel):
    worker: str = "mock"
    request: PlanRequestIn


class ReplayBody(BaseModel):
    run_id: str = Field(min_length=1)
    from_checkpoint: str = Field(min_length=1)


# ---------------------------------------------------------------- helpers ----
def _harness() -> Harness:
    # API runs with the default gate: a non-auto-recoverable safety failure
    # HALTS pending a human. Safety breaches are never auto-shipped here.
    return Harness(workspace_root=RUNS_DIR, max_attempts=MAX_ATTEMPTS)


def _write_request(raw: dict) -> str:
    import json, tempfile
    fd, path = tempfile.mkstemp(suffix=".json", prefix="req-")
    with os.fdopen(fd, "w") as f:
        json.dump(raw, f)
    return path


# ---------------------------------------------------------------- routes -----
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/meta")
def meta():
    """Everything the UI needs to render: declared guardrails, calorie floor,
    available workers."""
    return {
        "calorie_floor": G.SAFE_CALORIE_FLOOR,
        "calorie_ceiling": G.SANITY_CALORIE_CEILING,
        "max_attempts": MAX_ATTEMPTS,
        "workers": [
            {"id": "mock", "label": "Mock worker (deterministic — shows self-correction)",
             "available": True},
            {"id": "template", "label": "Template worker (alternate planner)", "available": True},
            {"id": "claude", "label": "Claude (live AI agent)", "available": _claude_available()},
            {"id": "gemini", "label": "Gemini (live AI agent — free tier)", "available": _gemini_available()},
        ],
        "guardrails": [
            {"id": g.id, "rule": g.rule, "severity": g.severity.value} for g in G.REGISTRY
        ],
        "checkpoints": [
            {"id": "CP1_SCHEMA", "criteria": "Proposal is structurally complete."},
            {"id": "CP2_SAFETY", "criteria": "No allergens; no calorie-floor breach."},
            {"id": "CP3_FIT", "criteria": "Diet respected; within budget."},
            {"id": "CP4_COMPLETENESS", "criteria": "Every ingredient confirmed or on shopping list."},
        ],
    }


@app.post("/api/plan")
def make_plan(body: PlanBody):
    worker = build_worker(body.worker)
    raw = body.request.to_raw()
    req_path = _write_request(raw)
    try:
        report = _harness().run(worker, req_path)
    finally:
        try:
            os.remove(req_path)
        except OSError:
            pass

    shopping = []
    if report.final_plan:
        shopping = sorted({i["name"][4:] for m in report.final_plan["meals"]
                           for i in m["ingredients"] if i["name"].startswith("buy:")})

    return JSONResponse({
        "run_id": report.run_id,
        "worker": report.worker,
        "attempts": report.attempts,
        "outcome": report.outcome.value,
        "shipped": report.outcome == Outcome.SHIPPED,
        "checkpoints": report.checkpoints,
        "alarms": report.alarms,
        "escalation": report.escalation,
        "final_plan": report.final_plan,
        "shopping_list": shopping,
    })


@app.post("/api/replay")
def replay(body: ReplayBody):
    try:
        results = _harness().replay_from(body.run_id, body.from_checkpoint)
    except FileNotFoundError:
        raise HTTPException(status_code=404,
                            detail=f"No snapshot for run '{body.run_id}' at '{body.from_checkpoint}'.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown checkpoint id.")
    return {"run_id": body.run_id, "from": body.from_checkpoint, "results": results}


# static UI (mounted last so /api/* wins)
@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
