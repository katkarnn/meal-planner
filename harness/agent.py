"""
THE WORKER (the agent that LIVES INSIDE the harness)
====================================================
The worker is deliberately thin and replaceable. The harness only requires the
`Worker` protocol: `propose(request, feedback) -> dict`. A worker NEVER writes
files, NEVER reads the pantry from disk, and NEVER decides what ships. It just
proposes, and re-proposes when handed feedback.

Three workers are provided to prove the interface is swappable (Should #1) and
portable (Bonus):
  - MockMealWorker     deterministic; ships defects on attempt 1, fixes them
                       when it receives structured guardrail feedback (Must #2)
  - TemplateMealWorker a structurally different worker, drops in unchanged
  - ClaudeMealWorker   the production path: a real Claude API call
"""
from __future__ import annotations

import json
import os
from typing import Protocol


class Worker(Protocol):
    name: str
    def propose(self, request, feedback: str) -> dict: ...


def _meal(day, slot, name, ings, cal, cost):
    return {"day": day, "slot": slot, "name": name, "calories": cal,
            "cost_cents": cost, "ingredients": ings}


def _confirm(name: str, qty: str, pantry: tuple[str, ...]) -> dict:
    """Flag any non-pantry ingredient for purchase so it can never read as a
    silent/unconfirmed assumption."""
    in_pantry = any(p in name.lower() or name.lower() in p for p in pantry)
    return {"name": name if in_pantry else f"buy:{name}", "qty": qty}


# --------------------------------------------------------------------------
# Worker A — deterministic mock that demonstrably reacts to feedback
# --------------------------------------------------------------------------
class MockMealWorker:
    name = "mock-meal-worker"

    def propose(self, request, feedback: str) -> dict:
        req = request
        fix_allergen = "ALLERGEN_EXCLUSION" in feedback
        fix_unconfirmed = "INGREDIENT_CONFIRMED" in feedback
        per_meal = max(req.daily_calorie_target // 3, 400)
        meals = []
        for day in range(1, req.days + 1):
            # breakfast — on attempt 1 this carries the allergen defect
            if fix_allergen:
                spread = _confirm("sunflower seed butter", "1 tbsp", req.pantry)
            else:
                spread = {"name": "peanut butter", "qty": "1 tbsp"}   # DEFECT: allergen
            meals.append(_meal(day, "breakfast", "Oatmeal bowl",
                               [_confirm("oats", "80g", req.pantry), spread,
                                _confirm("banana", "1", req.pantry)],
                               per_meal, 180))
            # lunch
            meals.append(_meal(day, "lunch", "Grain salad",
                               [_confirm("quinoa", "100g", req.pantry),
                                _confirm("chickpeas", "1 can", req.pantry),
                                _confirm("olive oil", "1 tbsp", req.pantry)],
                               per_meal, 320))
            # dinner — on attempt 1 'fresh basil' is left unflagged (defect)
            basil = _confirm("fresh basil", "handful", req.pantry) if fix_unconfirmed \
                else {"name": "fresh basil", "qty": "handful"}        # DEFECT: unconfirmed
            meals.append(_meal(day, "dinner", "Tomato pasta",
                               [_confirm("pasta", "120g", req.pantry),
                                _confirm("tomato sauce", "200g", req.pantry), basil],
                               req.daily_calorie_target - 2 * per_meal, 290))
        return {"request_user": req.user_id, "meals": meals,
                "notes": "deterministic mock proposal"}


# --------------------------------------------------------------------------
# Worker B — a structurally different worker (Bonus: prove portability)
# --------------------------------------------------------------------------
class TemplateMealWorker:
    name = "template-meal-worker"

    def propose(self, request, feedback: str) -> dict:
        req = request
        per_meal = max(req.daily_calorie_target // 3, 400)
        meals = []
        for day in range(1, req.days + 1):
            meals.append(_meal(day, "breakfast", "Greek yogurt & berries",
                               [_confirm("greek yogurt", "150g", req.pantry),
                                _confirm("blueberries", "80g", req.pantry),
                                _confirm("honey", "1 tsp", req.pantry)], per_meal, 210))
            meals.append(_meal(day, "lunch", "Rice & beans bowl",
                               [_confirm("brown rice", "100g", req.pantry),
                                _confirm("black beans", "1 can", req.pantry),
                                _confirm("avocado", "half", req.pantry)], per_meal, 260))
            meals.append(_meal(day, "dinner", "Baked salmon & veg",
                               [_confirm("salmon fillet", "150g", req.pantry),
                                _confirm("broccoli", "150g", req.pantry),
                                _confirm("lemon", "half", req.pantry)],
                               req.daily_calorie_target - 2 * per_meal, 430))
        return {"request_user": req.user_id, "meals": meals,
                "notes": "template-driven proposal (alternate worker)"}


# --------------------------------------------------------------------------
# Worker C — the production path: a real Claude API call
# --------------------------------------------------------------------------
class ClaudeMealWorker:
    name = "claude-meal-worker"

    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model = model

    def propose(self, request, feedback: str) -> dict:
        from anthropic import Anthropic   # imported lazily so the demo runs without the dep
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot run the live Claude worker.")
        client = Anthropic(api_key=key)
        req = request
        system = (
            "You are a meal-planning worker INSIDE a safety harness. You only PROPOSE. "
            "Return ONLY JSON, no prose, no code fences. Schema: "
            '{"request_user": str, "meals": [{"day": int, "slot": "breakfast|lunch|dinner", '
            '"name": str, "calories": int, "cost_cents": int, '
            '"ingredients": [{"name": str, "qty": str}]}]}. '
            "Prefix any ingredient not in the pantry with 'buy:'. "
            "STRONGLY prefer recipes that use the user's existing pantry items: feature at "
            "least one pantry ingredient on most days to minimize shopping trips and cost. "
            "Only buy new ingredients when the pantry can't satisfy the meal. "
            "Never violate the diet_type or allergens to use a pantry item."
        )
        user = {
            "days": req.days, "allergens": list(req.allergens), "diet_type": req.diet_type,
            "goal": req.goal, "daily_calorie_target": req.daily_calorie_target,
            "budget_cents": req.budget_cents, "pantry": list(req.pantry),
            "correction_feedback": feedback or "none",
        }
        msg = client.messages.create(
            model=self.model, max_tokens=2000, system=system,
            messages=[{"role": "user", "content": json.dumps(user)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)


# --------------------------------------------------------------------------
# Worker D — a free-tier alternative path: a real Google Gemini API call
# Mirrors ClaudeMealWorker exactly so the harness can't tell them apart.
# --------------------------------------------------------------------------
class GeminiMealWorker:
    name = "gemini-meal-worker"

    def __init__(self, model: str = "gemini-2.5-flash"):
        self.model = model

    def propose(self, request, feedback: str) -> dict:
        from google import genai             # imported lazily so the demo runs without the dep
        from google.genai import types
        key = os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY is not set; cannot run the live Gemini worker.")
        client = genai.Client(api_key=key)
        req = request
        system = (
            "You are a meal-planning worker INSIDE a safety harness. You only PROPOSE. "
            "Return ONLY JSON, no prose, no code fences. Schema: "
            '{"request_user": str, "meals": [{"day": int, "slot": "breakfast|lunch|dinner", '
            '"name": str, "calories": int, "cost_cents": int, '
            '"ingredients": [{"name": str, "qty": str}]}]}. '
            "Prefix any ingredient not in the pantry with 'buy:'. "
            "STRONGLY prefer recipes that use the user's existing pantry items: feature at "
            "least one pantry ingredient on most days to minimize shopping trips and cost. "
            "Only buy new ingredients when the pantry can't satisfy the meal. "
            "Never violate the diet_type or allergens to use a pantry item."
        )
        user = {
            "days": req.days, "allergens": list(req.allergens), "diet_type": req.diet_type,
            "goal": req.goal, "daily_calorie_target": req.daily_calorie_target,
            "budget_cents": req.budget_cents, "pantry": list(req.pantry),
            "correction_feedback": feedback or "none",
        }
        resp = client.models.generate_content(
            model=self.model,
            contents=json.dumps(user),
            config=types.GenerateContentConfig(
                system_instruction=system,
                # Gemini 2.5 Flash is a "thinking" model: it spends output tokens on
                # internal reasoning before the answer. Disable it so the full budget
                # goes to the JSON, and give the plan generous room so it never truncates.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=8000,
                response_mime_type="application/json",
            ),
        )
        text = (resp.text or "").replace("```json", "").replace("```", "").strip()
        return json.loads(text)
