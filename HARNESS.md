# HARNESS.md — Personal Meal-Planner Safety Harness

A harness that an AI meal-planning agent *lives inside*. The agent never writes
files and never decides what ships — it only **proposes** a weekly plan. The
harness sits in the gap between *proposal* and *application*, and that gap is
where all safety lives. Its job: guarantee a plan never reaches the user with
an allergen they react to, an unsafe calorie target, or an unconfirmed
ingredient.

## 1. Core idea: propose vs. apply

The worker is powerful but untrusted. It produces a `MealPlan` proposal and
hands it back. Nothing the worker says is authoritative. The harness is the
only component that can:

- declare what "safe" means (guardrails),
- gate the proposal against those rules (checkpoints),
- move material in and proposals out (material handling),
- raise structured signals when something is wrong (alarms),
- and decide to **ship**, **retry**, or **stop and ask a human**.

Because the worker only speaks `propose(request, feedback) -> proposal`, it is
fully swappable. The safety properties belong to the harness, not the model.

## 2. Architecture

```
            inputs/weekly_request.json          (real product input)
                        │
                        ▼
            ┌───────────────────────┐
            │  MATERIAL HANDLING     │  material.py
            │  PlanRequest  ─in──►   │  - typed, immutable request
            │  MealPlan     ◄─out─   │  - Workspace: persists snapshots
            └───────────┬───────────┘
                        │ PlanRequest + accumulated feedback
                        ▼
            ┌───────────────────────┐        ┌────────────────────┐
            │   WORKER (the agent)   │◄──────►│  feedback loop      │
            │   agent.py             │        │  (behavior change)  │
            │   propose()            │        └────────────────────┘
            └───────────┬───────────┘
                        │ MealPlan proposal
                        ▼
            ┌───────────────────────┐   reads   ┌──────────────────┐
            │   CHECKPOINTS          │──────────►│  GUARDRAILS      │
            │   checkpoints.py       │           │  guardrails.py   │
            │   CP1 SCHEMA           │           │  declared        │
            │   CP2 SAFETY  ◄────────┼─ critical │  REGISTRY        │
            │   CP3 FIT              │   gate    └──────────────────┘
            │   CP4 COMPLETENESS     │
            └───────────┬───────────┘ persists each result (replay)
                        │ raises
                        ▼
            ┌───────────────────────┐
            │   ALARMS               │  alarms.py
            │   typed / severity /   │
            │   context / action     │
            └───────────┬───────────┘
                        │ CRITICAL or non-recoverable
                        ▼
            ┌───────────────────────┐
            │   HUMAN-IN-THE-LOOP    │  harness.py
            │   EscalationPacket     │  stop & ask, don't guess
            └───────────────────────┘
                        │
                        ▼
                 SHIP  /  BLOCK
```

The orchestrator (`harness.py`) only *sequences* the pillars; it does not
reimplement any of them.

## 3. The four pillars

### Pillar 1 — Guardrails (`guardrails.py`)

Guardrails are **declared, not implicit**. Every constraint is a data object in
a single `REGISTRY` list with an id, a human-readable rule, a severity, and a
pure `check(plan, request) -> GuardrailResult`. There is no safety logic hidden
in the worker or orchestrator; tuning safety means editing this list.

| Guardrail | Severity | Rule |
|---|---|---|
| `ALLERGEN_EXCLUSION` | CRITICAL | No ingredient matching a user allergen (alias-expanded) may appear. |
| `CALORIE_FLOOR` | CRITICAL | No day, and no stated target, may fall below the 1200 kcal safety floor. |
| `CALORIE_CEILING` | WARNING | No day may exceed the 5000 kcal sanity ceiling. |
| `INGREDIENT_CONFIRMED` | WARNING | Every ingredient is pantry-stocked or flagged `buy:`. |
| `DIET_COMPLIANCE` | WARNING | All meals respect the declared diet type. |
| `BUDGET_LIMIT` | WARNING | Total cost stays within the weekly budget. |

A failed guardrail returns structured `violations` (the context alarms carry),
a worker-readable `feedback` string, and an `auto_recoverable` flag. That flag
is the line between "retry the worker" and "stop and ask a human" — see §5.

### Pillar 2 — Checkpoints (`checkpoints.py`)

Checkpoints are ordered gates with **explicit pass/fail criteria**. Each stage
states its criterion and returns `PASS`/`FAIL` plus the evidence it judged on.

1. `CP1_SCHEMA` — the proposal is structurally complete (every requested day
   has meals; every meal has ingredients and positive calories).
2. `CP2_SAFETY` — the safety gate: zero CRITICAL violations (no allergens, no
   calorie-floor breach).
3. `CP3_FIT` — diet type respected and total cost within budget.
4. `CP4_COMPLETENESS` — no unconfirmed ingredients; everything resolves to the
   pantry or the shopping list.

**Order is a safety property.** `CP2_SAFETY` runs before any stage that could
wave a plan through, and a CRITICAL failure *stops the line* so no downstream
stage ever evaluates — let alone ships — an unsafe plan.

Every checkpoint result is **persisted** by the `Workspace` as a JSON snapshot
of the plan + result. `Harness.replay_from(run_id, checkpoint_id)` reloads the
persisted plan at that checkpoint and re-runs the remaining stages **without
invoking the worker**.

### Pillar 3 — Material handling (`material.py`)

Clean, typed interfaces decouple the worker from everything else. The worker is
handed one immutable `PlanRequest` and returns a `MealPlan`; it never touches
disk or raw input. The `Workspace` owns a single run's material: it loads and
records the request, writes every checkpoint snapshot, and is the substrate for
replay. This decoupling is exactly what makes the worker swappable.

### Pillar 4 — Alarms (`alarms.py`)

Alarms are **structured**, not log lines. Each `Alarm` has a named `AlarmType`,
a `Severity`, a `context` payload, a `recommended_action`, and a `source`. They
stream to stderr as JSONL (greppable / machine-readable) and are collected per
run. `CRITICAL` alarms are *blocking* — they cannot be shipped past.

Named alarm types: `ALLERGEN_DETECTED`, `CALORIE_FLOOR_BREACH`,
`CALORIE_CEILING_BREACH`, `UNCONFIRMED_INGREDIENT`, `DIET_VIOLATION`,
`BUDGET_EXCEEDED`, `MALFORMED_PROPOSAL`, `WORKER_LOOP_EXHAUSTED`,
`HUMAN_ESCALATION_REQUIRED`.

## 4. Control flow & behavior change

The orchestrator runs an attempt loop. Each attempt: the worker proposes, the
checkpoint pipeline evaluates and persists, and any failed checkpoint
contributes its feedback to a **cumulative** feedback set. That accumulated
feedback is handed back to the worker on the next attempt, so its proposal
changes in response to the harness rather than oscillating.

The cumulative set matters: in the demo the worker first ships a plan with a
peanut spread (allergen) *and* an unflagged ingredient. The safety gate stops
the line on the allergen before completeness is even checked; only after the
allergen is removed does the completeness gate surface the unflagged
ingredient. By remembering both corrections, the worker converges to a safe,
shippable plan by attempt 3 instead of trading one defect for the other.

## 5. Human-in-the-loop

The harness knows when to stop and ask. A guardrail can mark a failure
`auto_recoverable = False` — e.g. a user's **stated** calorie target below the
safety floor is not something the harness should silently "fix" by inventing a
higher number. When that happens, the orchestrator builds an `EscalationPacket`
(reason, blocking alarms, checkpoint, attempt, plan digest, the decision
question) and calls the injected `human_gate`, which returns:

- `True` — human approves an override and the plan ships,
- `False` — human rejects; the plan is blocked,
- `None` — no human available; the run halts **pending review** (the safe
  default).

Loop exhaustion (worker can't satisfy the checkpoints within the attempt
budget) also escalates rather than shipping a best-effort guess.

## 6. Swappable workers & portability

The worker contract is the `Worker` protocol: `name` + `propose(request,
feedback) -> dict`. Three implementations ship:

- `MockMealWorker` — deterministic; demonstrates feedback-driven correction.
- `TemplateMealWorker` — a structurally different planner used to prove
  portability: it drops into the *same* harness with **zero harness changes**.
- `ClaudeMealWorker` — the production path. A real Claude API call
  (`claude-sonnet-4-6`) that returns the same proposal JSON. Set
  `ANTHROPIC_API_KEY` and pass it to `Harness.run()` exactly like the mocks.

## 7. The safety stance on calories

The calorie pillar is built as a **floor that blocks and escalates**, never a
prescriber. The 1200 kcal floor is a conservative, non-personalized backstop —
not medical advice. The harness refuses to ship plans below it and routes
unsafe targets to a human/professional rather than computing aggressive
deficits itself. Goal-setting is treated as a human decision, not a worker
output the harness rubber-stamps.

## 8. Running the demo

```bash
python3 demo.py          # no third-party deps required for the mock/template workers
# stderr carries the structured JSONL alarm stream:
python3 demo.py 2>alarms.log
```

Real input lives in `inputs/weekly_request.json` (a pescatarian user with
peanut + shellfish allergies, a maintenance target, and a weekly budget).
`inputs/unsafe_target_request.json` drives the escalation path.

Demo scenarios and what each proves:

- **A** — safe request: worker self-corrects over attempts and ships. *(all four pillars; behavior change)*
- **B** — unsafe stated target: harness stops and asks; human rejects. *(alarms; human-in-the-loop)*
- **C** — replay scenario A from `CP3_FIT` with no worker call. *(checkpoint persistence/replay)*
- **D** — swap `TemplateMealWorker` into the same pipeline. *(swappable interface; portability bonus)*

## 9. Requirements coverage

| Requirement | Where |
|---|---|
| **Must** four pillars, separate from worker | `guardrails.py`, `checkpoints.py`, `material.py`, `alarms.py`; worker isolated in `agent.py` |
| **Must** agent behavior changes on feedback | cumulative feedback loop in `harness.py`; Scenario A (3 attempts) |
| **Must** guardrails declared; checkpoints explicit pass/fail | `guardrails.REGISTRY`; `checkpoints.PIPELINE` with `criteria` |
| **Must** alarms structured (type/severity/context/action) | `alarms.Alarm` + `AlarmType` |
| **Must** runs on real input at demo | `inputs/weekly_request.json` |
| **Must** HARNESS.md | this file |
| **Should** swappable agent interface | `Worker` protocol; Scenario D |
| **Should** persisted checkpoints + replay | `Workspace.snapshot/restore`, `Harness.replay_from`; Scenario C |
| **Should** human-in-the-loop escalation | `human_gate`, `EscalationPacket`; Scenario B |
| **Bonus** second worker swapped at demo | `TemplateMealWorker`; Scenario D |

## 11. Web product

The harness is wrapped by a FastAPI server (`app/main.py`) and a single-page UI
(`web/index.html`). The API is a thin shell: it validates input, selects a
worker, calls `Harness.run`, and returns the structured `RunReport`. All safety
decisions remain in the harness. Endpoints: `GET /api/meta` (declared
guardrails + workers), `POST /api/plan`, `POST /api/replay`, `GET /healthz`.
The UI renders the four-gate pipeline, the ship/block/await-human verdict, the
alarm stream, the plan + shopping list, and replay. In the web context the
human gate halts on a non-auto-recoverable safety failure rather than
auto-shipping; the UI directs the user to fix the input. See `README.md` for
running locally on Windows and shipping to Railway.

## 12. Layout (current)

```
harness/
  __init__.py        public API
  material.py        Pillar 3 — typed material + Workspace persistence
  guardrails.py      Pillar 1 — declared constraint registry
  checkpoints.py     Pillar 2 — ordered pass/fail pipeline
  alarms.py          Pillar 4 — structured alarm taxonomy + sink
  agent.py           the swappable worker(s)
  harness.py         orchestrator: loop, gating, escalation, replay
demo.py              runs all scenarios on real input
inputs/              real product inputs
runs/                per-run material + checkpoint snapshots (generated)
```
