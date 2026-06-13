"""
PILLAR 1 — GUARDRAILS
=====================
Guardrails are DECLARED, not implicit (Must #3). Each one is a data object in
the registry below with an id, a severity, a human-readable rule, and a pure
`check(plan, request) -> GuardrailResult`. Adding or tuning a constraint means
editing this registry — there is no safety logic hidden inside the worker or
the orchestrator.

A guardrail does three things when violated:
  1. records structured violations (context for alarms),
  2. emits a worker-readable `feedback` string (this is what makes the agent
     change behavior — Must #2),
  3. tells the harness whether it is auto-recoverable or needs a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .alarms import Alarm, AlarmType, Severity
from .material import MealPlan, PlanRequest


# Conservative, NON-personalized safety floor. This is a backstop to block
# dangerously low targets and hand them to a human/professional — it is not a
# prescription or medical advice. Configurable per deployment.
SAFE_CALORIE_FLOOR = 1200
SANITY_CALORIE_CEILING = 5000

# Allergen expansion so derived/co-named ingredients are also caught. Keys are
# food-TYPE categories (matching the UI checkboxes); each maps to the concrete
# ingredient terms the allergen guardrail scans for.
ALLERGEN_ALIASES: dict[str, tuple[str, ...]] = {
    "peanut": ("peanut", "peanuts", "peanut butter", "groundnut", "arachis"),
    "tree nut": ("almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio",
                 "macadamia", "brazil nut", "pine nut", "nut butter"),
    "dairy": ("milk", "butter", "cream", "cheese", "whey", "casein", "yogurt",
              "ghee", "custard", "ice cream"),
    "egg": ("egg", "eggs", "albumin", "mayonnaise", "meringue"),
    "soy": ("soy", "soya", "tofu", "edamame", "tempeh", "miso"),
    "gluten": ("wheat", "barley", "rye", "gluten", "flour", "bread", "pasta",
               "couscous", "cracker", "noodle", "tortilla"),
    "shellfish": ("shrimp", "prawn", "crab", "lobster", "crayfish", "shellfish",
                  "scallop", "clam", "mussel", "oyster", "squid"),
    "fish": ("fish", "salmon", "tuna", "cod", "tilapia", "sardine", "anchovy",
             "haddock", "mackerel", "trout", "halibut"),
    "sesame": ("sesame", "tahini"),
    "poultry": ("chicken", "turkey", "duck", "poultry", "hen", "quail"),
    "pork": ("pork", "bacon", "ham", "prosciutto", "sausage", "chorizo", "salami"),
    "red meat": ("beef", "lamb", "mutton", "veal", "steak", "venison", "goat"),
    "oil": ("oil", "shortening", "lard", "margarine"),
    "mustard": ("mustard",),
    "corn": ("corn", "maize", "cornstarch", "cornflour", "polenta", "cornmeal"),
    "nightshade": ("tomato", "potato", "eggplant", "aubergine", "bell pepper",
                   "paprika", "chili", "cayenne"),
}

DIET_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "vegetarian": ("chicken", "beef", "pork", "fish", "salmon", "tuna", "shrimp", "bacon"),
    "vegan": ("chicken", "beef", "pork", "fish", "salmon", "egg", "milk", "cheese", "butter", "honey"),
    "pescatarian": ("chicken", "beef", "pork", "bacon", "lamb"),
}


@dataclass
class GuardrailResult:
    guardrail_id: str
    ok: bool
    severity: Severity
    violations: list[dict] = field(default_factory=list)
    feedback: str = ""                 # fed back to the worker on failure
    auto_recoverable: bool = True      # False -> harness must escalate to a human
    alarm_type: AlarmType | None = None


@dataclass
class Guardrail:
    id: str
    rule: str                          # declared, human-readable
    severity: Severity
    check: Callable[[MealPlan, PlanRequest], GuardrailResult]


# --------------------------------------------------------------------------
# Individual guardrail checks
# --------------------------------------------------------------------------
def _expand_allergens(allergens: tuple[str, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for a in allergens:
        terms = set(ALLERGEN_ALIASES.get(a, (a,)))
        terms.add(a)
        out[a] = {t.lower() for t in terms}
    return out


def check_allergens(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    expanded = _expand_allergens(req.allergens)
    violations = []
    for meal, ing in plan.all_ingredients():
        for allergen, terms in expanded.items():
            if any(t in ing.key for t in terms):
                violations.append({
                    "day": meal.day, "slot": meal.slot, "meal": meal.name,
                    "ingredient": ing.name, "allergen": allergen,
                })
    if violations:
        bad = ", ".join(f"{v['ingredient']} (={v['allergen']}) in '{v['meal']}'" for v in violations)
        return GuardrailResult(
            "ALLERGEN_EXCLUSION", ok=False, severity=Severity.CRITICAL,
            violations=violations,
            feedback=f"REMOVE all allergen ingredients. The user reacts to {req.allergens}. "
                     f"Replace these meals/ingredients: {bad}.",
            auto_recoverable=True, alarm_type=AlarmType.ALLERGEN_DETECTED,
        )
    return GuardrailResult("ALLERGEN_EXCLUSION", ok=True, severity=Severity.CRITICAL)


def check_calorie_floor(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    # Two breaches matter: the user's STATED target, and the actual plan totals.
    violations = []
    if req.daily_calorie_target < SAFE_CALORIE_FLOOR:
        violations.append({"kind": "stated_target", "value": req.daily_calorie_target,
                           "floor": SAFE_CALORIE_FLOOR})
    for day, kcal in plan.calories_by_day().items():
        if kcal < SAFE_CALORIE_FLOOR:
            violations.append({"kind": "plan_day", "day": day, "value": kcal,
                               "floor": SAFE_CALORIE_FLOOR})
    if violations:
        # A target below the medical-supervision floor is NOT something the
        # harness should silently "fix" by inventing a higher number.
        stated = any(v["kind"] == "stated_target" for v in violations)
        return GuardrailResult(
            "CALORIE_FLOOR", ok=False, severity=Severity.CRITICAL, violations=violations,
            feedback=f"Daily calories must not fall below the {SAFE_CALORIE_FLOOR} kcal safety floor.",
            auto_recoverable=not stated,   # stated unsafe target -> escalate to a human
            alarm_type=AlarmType.CALORIE_FLOOR_BREACH,
        )
    return GuardrailResult("CALORIE_FLOOR", ok=True, severity=Severity.CRITICAL)


def check_calorie_ceiling(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    violations = [{"day": d, "value": k} for d, k in plan.calories_by_day().items()
                  if k > SANITY_CALORIE_CEILING]
    if violations:
        return GuardrailResult(
            "CALORIE_CEILING", ok=False, severity=Severity.WARNING, violations=violations,
            feedback=f"Daily calories exceed the {SANITY_CALORIE_CEILING} kcal sanity ceiling; rebalance.",
            alarm_type=AlarmType.CALORIE_CEILING_BREACH,
        )
    return GuardrailResult("CALORIE_CEILING", ok=True, severity=Severity.WARNING)


def check_ingredient_confirmed(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    # Every ingredient must be either in the confirmed pantry OR explicitly
    # flagged for purchase via the "buy:" prefix. Silent assumptions are banned.
    unconfirmed = []
    for meal, ing in plan.all_ingredients():
        key = ing.key
        flagged_to_buy = key.startswith("buy:")
        in_pantry = any(p in key or key in p for p in req.pantry)
        if not (flagged_to_buy or in_pantry):
            unconfirmed.append({"day": meal.day, "meal": meal.name, "ingredient": ing.name})
    if unconfirmed:
        names = ", ".join(sorted({u["ingredient"] for u in unconfirmed}))
        return GuardrailResult(
            "INGREDIENT_CONFIRMED", ok=False, severity=Severity.WARNING, violations=unconfirmed,
            feedback=f"These ingredients are neither in the pantry nor flagged for purchase: {names}. "
                     f"Prefix each unstocked item name with 'buy:' so it lands on the shopping list.",
            alarm_type=AlarmType.UNCONFIRMED_INGREDIENT,
        )
    return GuardrailResult("INGREDIENT_CONFIRMED", ok=True, severity=Severity.WARNING)


def check_diet(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    forbidden = DIET_FORBIDDEN.get(req.diet_type, ())
    violations = []
    for meal, ing in plan.all_ingredients():
        for f in forbidden:
            if f in ing.key:
                violations.append({"meal": meal.name, "ingredient": ing.name, "forbidden": f})
    if violations:
        bad = ", ".join(f"{v['ingredient']} in '{v['meal']}'" for v in violations)
        return GuardrailResult(
            "DIET_COMPLIANCE", ok=False, severity=Severity.WARNING, violations=violations,
            feedback=f"Diet is '{req.diet_type}'. Remove non-compliant items: {bad}.",
            alarm_type=AlarmType.DIET_VIOLATION,
        )
    return GuardrailResult("DIET_COMPLIANCE", ok=True, severity=Severity.WARNING)


def check_budget(plan: MealPlan, req: PlanRequest) -> GuardrailResult:
    total = plan.total_cost_cents()
    if total > req.budget_cents:
        return GuardrailResult(
            "BUDGET_LIMIT", ok=False, severity=Severity.WARNING,
            violations=[{"total_cents": total, "budget_cents": req.budget_cents}],
            feedback=f"Plan costs ${total/100:.2f} but budget is ${req.budget_cents/100:.2f}. "
                     f"Substitute cheaper ingredients.",
            alarm_type=AlarmType.BUDGET_EXCEEDED,
        )
    return GuardrailResult("BUDGET_LIMIT", ok=True, severity=Severity.WARNING)


# --------------------------------------------------------------------------
# THE DECLARED REGISTRY  (this is the whole guardrail surface)
# --------------------------------------------------------------------------
REGISTRY: list[Guardrail] = [
    Guardrail("ALLERGEN_EXCLUSION",
              "No meal may contain any ingredient the user reacts to (incl. aliases).",
              Severity.CRITICAL, check_allergens),
    Guardrail("CALORIE_FLOOR",
              f"No day, and no stated target, may fall below the {SAFE_CALORIE_FLOOR} kcal safety floor.",
              Severity.CRITICAL, check_calorie_floor),
    Guardrail("CALORIE_CEILING",
              f"No day may exceed the {SANITY_CALORIE_CEILING} kcal sanity ceiling.",
              Severity.WARNING, check_calorie_ceiling),
    Guardrail("INGREDIENT_CONFIRMED",
              "Every ingredient is in the pantry or explicitly flagged for purchase.",
              Severity.WARNING, check_ingredient_confirmed),
    Guardrail("DIET_COMPLIANCE",
              "All meals respect the declared diet type.",
              Severity.WARNING, check_diet),
    Guardrail("BUDGET_LIMIT",
              "Total plan cost does not exceed the weekly budget.",
              Severity.WARNING, check_budget),
]


def evaluate_all(plan: MealPlan, req: PlanRequest) -> list[GuardrailResult]:
    return [g.check(plan, req) for g in REGISTRY]


def build_feedback(results: list[GuardrailResult]) -> str:
    """Concatenate worker-readable feedback from every failed guardrail. This
    string is handed back to the worker so its next proposal changes."""
    return "\n".join(f"- [{r.guardrail_id}] {r.feedback}" for r in results if not r.ok and r.feedback)
