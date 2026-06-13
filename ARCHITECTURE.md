# Meal-Planner Safety Harness — High-Level Design

> A safety harness that an AI meal-planning **agent lives inside**. The agent only *proposes*; the harness alone decides what reaches the user. All safety lives in the gap between **propose** and **apply**.

---

## 1. The real-world problem

People increasingly let AI plan what they eat. But a meal plan is not a chatbot reply — it is acted on. An AI that is *fluent* is not the same as an AI that is *safe*:

| A confident AI can output… | …and a real person gets hurt |
|---|---|
| Peanut butter for a peanut-allergic user | Anaphylaxis |
| A 700 kcal/day "weight-loss" plan | Starvation-level diet, medically unsafe |
| An ingredient nobody confirmed you own | A plan you literally can't cook |

The model produces these confidently, with no flag. **You cannot trust the model to police itself** — the thing generating the plan can't also be the final word on whether it's safe to serve.

## 2. How we address it — *propose vs. apply*

We don't try to build a "safer model." We wrap **any** model in a harness that owns every safety decision. The agent is treated as **powerful but untrusted**: it makes proposals, and nothing it says is authoritative until the harness clears it.

```mermaid
flowchart LR
    U([User request]) --> H
    subgraph H[THE HARNESS  — owns all safety]
        direction TB
        A[AI Agent<br/>proposes only] -->|MealPlan proposal| G{Gates}
        G -->|fails| FB[Structured feedback]
        FB -->|re-propose| A
        G -->|passes| S([SHIP ✅])
        G -->|unsafe / stuck| HU([STOP → ask a human 🙋])
    end
    style A fill:#e3f2fd,stroke:#1976d2
    style G fill:#fff3e0,stroke:#f57c00
    style S fill:#e8f5e9,stroke:#388e3c
    style HU fill:#fce4ec,stroke:#c2185b
```

The agent is fully **swappable** because it only speaks one contract: `propose(request, feedback) → plan`. Swap Claude for Gemini for a mock — the safety properties don't move, because they were never in the model.

## 3. System architecture

```mermaid
flowchart TB
    subgraph CLIENT[Web UI · web/index.html]
        UI[Single-page app:<br/>request form · live gate view · verdict · replay]
    end
    subgraph API[API shell · FastAPI · app/main.py]
        EP["/api/plan · /api/meta · /api/replay · /healthz/"]
    end
    subgraph ORCH[Orchestrator · harness.py]
        LOOP[Attempt loop + decision logic<br/>ship / retry / escalate]
    end
    subgraph PILLARS[The four pillars — safety, not the model]
        P3[📦 Material<br/>material.py<br/>typed request/plan + Workspace]
        AG[🤖 Worker / Agent<br/>agent.py<br/>Claude · Gemini · Mock · Template]
        P1[🛡️ Guardrails<br/>guardrails.py<br/>declared REGISTRY]
        P2[✅ Checkpoints<br/>checkpoints.py<br/>ordered pass/fail gates]
        P4[🚨 Alarms<br/>alarms.py<br/>structured + severity]
    end
    DISK[(runs/ · per-run snapshots<br/>→ enables replay)]

    UI --> EP --> LOOP
    LOOP --> P3 --> AG
    AG -->|proposal| LOOP
    LOOP --> P2
    P2 -->|reads rules| P1
    P2 -->|raises| P4
    P4 -->|CRITICAL / non-recoverable| LOOP
    P2 -.persists each result.-> DISK

    style AG fill:#e3f2fd,stroke:#1976d2
    style P1 fill:#fff3e0,stroke:#f57c00
    style P2 fill:#fff3e0,stroke:#f57c00
    style P4 fill:#ffebee,stroke:#c62828
    style ORCH fill:#f3e5f5,stroke:#7b1fa2
```

**The orchestrator only *sequences* the pillars — it reimplements none of them.** That separation is the whole design: safety is composed from four small, independently-true parts.

| Pillar | Role | Key property |
|---|---|---|
| 🛡️ **Guardrails** | *Declare* what "safe" means | Every rule is a data object in one `REGISTRY` — no safety logic hidden in the model |
| ✅ **Checkpoints** | *Gate* the proposal in order | Explicit PASS/FAIL; **SAFETY runs before anything can ship** |
| 📦 **Material** | Move request in / proposal out | Typed & immutable; agent never touches disk → swappable |
| 🚨 **Alarms** | *Signal* what went wrong | Structured (type · severity · context · action), not log lines |

## 4. How the agent works — and how it communicates

The agent never gets the last word, and it never works blind. Each time it fails a gate, the harness hands back a **structured, worker-readable feedback string** describing exactly what to fix. Feedback is **cumulative** — the harness remembers every correction so the agent converges instead of trading one defect for another.

```mermaid
sequenceDiagram
    participant H as Harness
    participant A as Agent (Claude/Gemini)
    participant G as Checkpoint Pipeline
    participant Hu as Human

    Note over H,G: Attempt 1
    H->>A: propose(request, feedback="")
    A-->>H: plan (has peanut + unflagged item)
    H->>G: run gates in order
    G-->>H: CP2_SAFETY ❌ allergen → line STOPS here
    Note right of G: unsafe plan never<br/>reaches later gates

    Note over H,G: Attempt 2 (feedback accumulates)
    H->>A: propose(request, "remove peanut…")
    A-->>H: plan (peanut fixed, item still unflagged)
    H->>G: run gates
    G-->>H: CP4 ❌ unconfirmed ingredient

    Note over H,G: Attempt 3
    H->>A: propose(request, "remove peanut… + flag item buy:")
    A-->>H: plan (both fixed)
    H->>G: run gates
    G-->>H: ALL PASS ✅
    H-->>H: SHIP

    Note over H,Hu: If a failure is non-recoverable<br/>(e.g. stated 700 kcal target)
    H->>Hu: EscalationPacket — "approve override or reject?"
    Hu-->>H: reject → BLOCK (never auto-guesses)
```

This is the core behavioral guarantee: **the agent's behavior changes in response to the harness**, not the other way around.

## 5. How the final outcome is produced

Every run ends in exactly one verdict. Safety gates **stop the line** — a CRITICAL failure means no later gate even runs, so an unsafe plan can never be "saved" by a downstream check.

```mermaid
flowchart TD
    START([Worker proposes plan]) --> CP1{CP1 · Schema<br/>well-formed?}
    CP1 -->|no| RETRY
    CP1 -->|yes| CP2{CP2 · Safety<br/>allergens? calorie floor?}
    CP2 -->|CRITICAL breach| STOP[⛔ line stops]
    CP2 -->|stated unsafe target| ESC
    CP2 -->|ok| CP3{CP3 · Fit<br/>diet + budget?}
    CP3 -->|no| RETRY
    CP3 -->|ok| CP4{CP4 · Completeness<br/>every ingredient confirmed?}
    CP4 -->|no| RETRY
    CP4 -->|ok| SHIP([✅ SHIPPED])

    STOP --> RETRY[Accumulate feedback]
    RETRY --> BUDGET{attempts left?}
    BUDGET -->|yes| START
    BUDGET -->|no| ESC[🙋 Escalate to human]
    ESC --> DEC{Human gate}
    DEC -->|approve| SHIP
    DEC -->|reject| BLOCKR([⛔ BLOCKED · rejected])
    DEC -->|no human| BLOCKP([⏸️ BLOCKED · pending review])

    style SHIP fill:#e8f5e9,stroke:#388e3c
    style BLOCKR fill:#fce4ec,stroke:#c2185b
    style BLOCKP fill:#fff3e0,stroke:#f57c00
    style ESC fill:#fce4ec,stroke:#c2185b
    style CP2 fill:#fff3e0,stroke:#f57c00
```

| Outcome | Meaning |
|---|---|
| **SHIPPED** | Cleared every gate (possibly after self-correction) |
| **BLOCKED · pending human** | Non-recoverable safety issue, no human wired in → safe default: halt |
| **BLOCKED · rejected** | A human reviewed and declined |
| **BLOCKED · loop exhausted** | Agent couldn't satisfy constraints in budget → escalates, never ships a guess |

Every checkpoint result is **persisted to disk**, so any run can be **replayed from any checkpoint forward without re-invoking the agent** — making safety decisions auditable and reproducible.

## 6. Technology — how it's built

```mermaid
flowchart LR
    subgraph Frontend
        F[HTML + vanilla JS<br/>single page, zero build]
    end
    subgraph Backend
        B[Python 3.12 · FastAPI · Uvicorn<br/>Pydantic validation]
    end
    subgraph Agents
        C[Anthropic Claude SDK]
        GE[Google Gemini SDK]
        M[Mock / Template<br/>no API key needed]
    end
    subgraph Deploy
        D[GitHub → Railway<br/>Nixpacks · Procfile]
    end
    Frontend --> Backend --> Agents
    Backend --> Deploy
```

- **Backend:** Python 3.12, FastAPI + Uvicorn, Pydantic for request validation. The core harness is **pure Python with zero third-party deps** — the AI SDKs are imported lazily, so the whole safety engine runs (and is testable) without any API key.
- **Agents:** Claude (`claude-sonnet-4-6`) and Gemini (`gemini-2.5-flash`) as live workers; deterministic Mock + Template workers prove the interface is swappable and demo-able offline.
- **Frontend:** one static HTML page — renders the live gate pipeline, verdict, alarm stream, plan, shopping list, and replay.
- **Deploy:** committed to GitHub, deployed to **Railway** (Nixpacks build, `Procfile` start command, API keys injected as environment variables — never committed).

---

### The one-line pitch

**Don't make the model safe — put the model in a harness that's safe.** The agent proposes; declared guardrails, ordered checkpoints, and a stop-and-ask-a-human gate decide. Swap the model freely; the guarantees never move.
