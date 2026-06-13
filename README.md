# Meal-Planner Safety Harness — runnable product

A web app where an AI agent **proposes** a weekly meal plan and a safety
**harness** decides whether it can ship. A plan never reaches the user with an
allergen they react to, a calorie target below the safety floor, or an
unconfirmed ingredient. The four pillars (guardrails, checkpoints, material
handling, alarms), the swappable workers, persistence/replay, and
human-in-the-loop escalation are all described in `HARNESS.md`.

This package is the harness **plus** a FastAPI server and a single-page UI, so
you can run it locally and ship it to a public URL.

```
app/main.py        FastAPI server (thin shell over the harness)
web/index.html     single-page UI (no build step, no CDN)
harness/           the four-pillar harness package
inputs/            sample requests
tests/             pytest suite (9 tests)
Procfile, railway.json, requirements.txt, .python-version
```

---

## 1. Install & run on Windows

**Prerequisite:** Python 3.12. Install from <https://www.python.org/downloads/>
and tick **“Add python.exe to PATH”** during setup. Verify in a new PowerShell:

```powershell
python --version    # should print 3.12.x
```

**Run it** (PowerShell, from the unzipped project folder):

```powershell
cd meal-planner-safety-harness

# create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
#  If activation is blocked by execution policy, run this once first:
#  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

pip install -r requirements.txt

# start the server
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. You’ll see the request form on the left, the
four-gate pipeline and verdict on the right.

To stop: `Ctrl+C`. To run again later: re-activate the venv and run the
`uvicorn` line.

### Optional: turn on the live AI agent locally

The **mock** and **template** workers need no key. To enable the **Claude**
worker (a real AI agent), set your Anthropic API key *before* starting the
server:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
uvicorn app.main:app --reload --port 8000
```

The “Claude (live AI agent)” option in the UI becomes selectable once the key
is present.

---

## 2. Ship to Railway (free) and demo on a live URL

Railway gives new accounts a **one-time $5 trial credit, no credit card
required** — comfortably enough for a live team demo. (It is a trial credit,
not a permanent free tier; the app stops once the credit is exhausted. See §5
for a $0 always-available alternative.)

**Fastest path — deploy from GitHub:**

1. Put this project in a GitHub repo:
   ```powershell
   git init
   git add .
   git commit -m "Meal-planner safety harness"
   git branch -M main
   git remote add origin https://github.com/<you>/meal-planner-safety-harness.git
   git push -u origin main
   ```
2. Go to <https://railway.com> → sign in with GitHub → **New Project** →
   **Deploy from GitHub repo** → pick the repo.
3. Railway auto-detects Python (Nixpacks), installs `requirements.txt`, and
   starts the app using the `startCommand` in `railway.json`
   (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`). No Dockerfile needed.
4. **Get the public URL:** open the service → **Settings → Networking →
   Generate Domain**. Railway gives you `https://<name>.up.railway.app` with
   HTTPS. That’s the link you share with your team.
5. **(Optional) live agent:** open **Variables** → add
   `ANTHROPIC_API_KEY = sk-ant-...` → the service redeploys and the Claude
   worker turns on.

**CLI alternative** (if you prefer not to use GitHub):

```powershell
npm i -g @railway/cli
railway login
railway init
railway up
railway domain          # prints your public URL
```

The app already binds to `0.0.0.0:$PORT`, which is what Railway requires — no
code changes needed.

---

## 3. Sample data & end-to-end test

Sample requests live in `inputs/`. In the UI they’re wired to the three preset
chips at the top of the form. Run these in order to exercise every pillar:

| Preset | Worker | Expected verdict | What it proves |
|---|---|---|---|
| **Safe request** | Mock | **SHIPPED** in ~3 attempts; all 4 gates green; shopping list = `fresh basil` | Guardrail/checkpoint feedback changes the agent’s behavior until the plan is safe |
| **Safe request** → then **Replay from CP3_FIT** | — | `CP3_FIT PASS`, `CP4_COMPLETENESS PASS` (no worker call) | Checkpoints are persisted and replayable |
| **Unsafe target** (900 kcal) | Mock | **AWAITING HUMAN**; `CALORIE_FLOOR_BREACH` (CRITICAL); escalation panel | Harness stops and asks instead of guessing |
| **Allergy-heavy** (incl. gluten) | Mock or Template | **BLOCKED / AWAITING HUMAN** | A worker that can’t satisfy the constraints never forces an unsafe plan through |
| **Allergy-heavy** | **Claude (live)** | **SHIPPED** | The real AI agent adapts to satisfy what the simple workers can’t |
| any preset | Template | swaps in with **zero harness changes** | Portability (bonus) |

You can also hit the API directly (works locally or against the Railway URL):

```powershell
curl -X POST https://<your-app>/api/plan `
  -H "Content-Type: application/json" `
  -d '{ "worker": "mock", "request": '"$(Get-Content inputs/weekly_request.json -Raw)"' }'
```

Endpoints: `GET /api/meta`, `POST /api/plan`, `POST /api/replay`,
`GET /healthz`.

---

## 4. Run the tests

```powershell
pip install pytest
python -m pytest -q        # 9 tests: guardrails, critical-stop ordering,
                           # behavior change, escalation, portability, replay
```

---

## 5. $0 always-on alternative (Render)

If you’d rather not rely on Railway’s trial credit, Render has a genuinely free
web-service tier (no credit card). The catch: it spins down after ~15 minutes
idle and takes ~30–50 seconds to wake on the next request — fine for a demo.

1. <https://render.com> → **New → Web Service** → connect the GitHub repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Instance type: **Free**. Add `ANTHROPIC_API_KEY` under Environment if you
   want the live agent.

---

## 6. Requirements coverage (from HARNESS.md)

| Requirement | Where |
|---|---|
| Four pillars, separate from the worker | `harness/guardrails.py`, `checkpoints.py`, `material.py`, `alarms.py`; worker in `harness/agent.py` |
| Agent behavior changes on feedback | cumulative feedback loop in `harness/harness.py`; **Safe request** ships after self-correction |
| Guardrails declared; checkpoints explicit pass/fail | `guardrails.REGISTRY`; `checkpoints.PIPELINE` with criteria |
| Alarms structured (type/severity/context/action) | `harness/alarms.py`; rendered in the UI alarm stream |
| Runs on real input at demo | `inputs/*.json` + the UI form |
| HARNESS.md | included |
| Swappable agent interface | `Worker` protocol; worker selector in the UI |
| Persisted checkpoints + replay | `Workspace` + `Harness.replay_from`; **Replay** buttons |
| Human-in-the-loop escalation | escalation packet surfaced in the UI; safety stops never auto-ship |
| Second worker swapped at demo (bonus) | Template worker, selectable live |

**Safety note:** the calorie pillar is a floor that **blocks and escalates**,
never a prescriber. It refuses targets below the configured floor and routes
them to a human rather than inventing numbers. These floors are conservative
safety backstops, not medical advice.
