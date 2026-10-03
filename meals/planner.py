"""
Creates a week's menu with the OpenAI API (Responses API).

The request runs in a background thread (a menu takes a minute or two). The model gets
the family profile, rules, household settings, recent menus with feedback and the
meals to plan, may search the web for recipes, and answers by calling the
`save_menu` function, whose strict schema guarantees well-formed data. The result is
saved as ordinary dishes, ingredients and planned meals, so the shopping list
follows automatically.
"""
import json
import logging
import threading
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import Category, Dish, FamilyMember, Feedback, Household, Ingredient, MenuRequest, PlannedMeal, Rule

logger = logging.getLogger(__name__)

MAX_TURNS = 3  # API calls per request: the first one, plus reminders to save the menu
STALE_AFTER = timedelta(minutes=15)

SYSTEM_PROMPT = """You plan the weekly meals for a family and write the menu into their meal planning app.

How to plan:
- Plan exactly the meals listed under "Meals to plan" - no more, no fewer - for the people listed with each meal. Meals listed under "Already planned" stay as they are; plan around them.
- Follow the family's rules and household settings. Respect every allergy and "won't eat" strictly; treat dislikes as strong preferences.
- Learn from the history: repeat dishes that were loved when it fits, avoid ones that were disliked or caused a bad reaction, and don't repeat a dish from the last three weeks unless the notes ask for it. Vary cuisines and proteins across the week.
- Weekday meals should fit the weekday cooking time; save longer cooking for the weekend.
- Plan leftovers where they genuinely help (for example a weekend lunch from the previous dinner): mark that meal as leftovers, give it no ingredients, and cook enough extra portions in the original meal.
- Prefer seasonal ingredients for the date and reuse ingredients across meals to keep the shopping list short and avoid waste.
- Pay close attention to the notes for this week.

Recipes:
- Use web search to find a real recipe page for each new dish, preferably from well-known recipe sites. Only use URLs that appeared in your search results; if you can't find a good one, leave recipe_url empty. Dishes listed with a recipe link in the history can keep that link without searching.
- Keep searching efficient: a few targeted searches, not one per ingredient.

Ingredients:
- List every ingredient to buy for the meal, with quantities for the number of portions you give in "servings" (people eating, plus any extra portions cooked for planned leftovers). Children eat roughly an adult portion unless their age suggests otherwise.
- Use metric units (g, kg, ml, l) or pcs, can, bunch, head, clove, tbsp, tsp, pack, jar. Use the same name for the same ingredient across meals (e.g. always "Onions").
- Include pantry staples the recipe needs too; the app sorts out what the family usually has.

When the plan is ready, call save_menu once with all meals. Write the summary for the parents: two to four sentences on how the week is balanced and anything they should know (for example what to prepare ahead)."""

KINDS = [k for k, _ in Dish.Kind.choices]
CATEGORIES = [c for c, _ in Category.choices]

SAVE_MENU_TOOL = {
    "type": "function",
    "name": "save_menu",
    "description": "Save the planned menu for the week into the app. Call this once, with every meal to plan.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "meals"],
        "properties": {
            "summary": {"type": "string", "description": "Two to four sentences for the parents about this week's menu."},
            "meals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["date", "slot", "dish_name", "kind", "minutes", "recipe_url", "leftovers",
                                 "servings", "note", "ingredients"],
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "slot": {"type": "string", "enum": ["lunch", "dinner"]},
                        "dish_name": {"type": "string", "description": "Short dish name, e.g. 'Salmon & pea pasta'. For leftovers, the name of the dish being eaten again."},
                        "kind": {"type": "string", "enum": KINDS},
                        "minutes": {"type": "integer", "description": "Active and cooking time; 0 for leftovers."},
                        "recipe_url": {"type": "string", "description": "Recipe page from your search results, or empty."},
                        "leftovers": {"type": "boolean", "description": "True when this meal is eaten from an earlier meal's leftovers."},
                        "servings": {"type": "integer", "description": "Portions to cook, including extra for planned leftovers. 0 for leftovers."},
                        "note": {"type": "string", "description": "One short practical tip shown on the menu, or empty."},
                        "ingredients": {
                            "type": "array",
                            "description": "Empty for leftovers.",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "quantity", "unit", "category", "note"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "quantity": {"type": ["number", "null"]},
                                    "unit": {"type": "string"},
                                    "category": {"type": "string", "enum": CATEGORIES},
                                    "note": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

def web_search_tool():
    # Without a location the search defaults to the United States; use the app's time zone instead.
    return {"type": "web_search", "user_location": {"type": "approximate", "timezone": settings.TIME_ZONE}}


class PlanningError(Exception):
    """Something the parents should see, e.g. 'The AI declined the request'."""


def enabled():
    return bool(settings.OPENAI_API_KEY)


def get_client():
    import openai

    # Reasoning plus web searches can take several minutes in one call.
    return openai.OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=3, timeout=900)


# --- prompt ------------------------------------------------------------------


def _member_line(member, today):
    age = member.age(today)
    head = f"- {member.name} ({member.get_kind_display().lower()}{f', {age}' if age is not None else ''})"
    details = [
        f"{label}: {value}"
        for label, value in [
            ("favourites", member.likes), ("dislikes", member.dislikes), ("won't eat", member.avoid),
            ("ALLERGIES/INTOLERANCES", member.allergies), ("notes", member.notes),
        ]
        if value.strip()
    ]
    return head + (": " + "; ".join(details) if details else "")


def _feedback_text(review):
    if not review:
        return ""
    parts = []
    if review.kids:
        parts.append(f"kids: {review.get_kids_display().split(' ', 1)[1].lower()}")
    if review.parents:
        parts.append(f"parents: {review.get_parents_display().split(' ', 1)[1].lower()}")
    if review.reaction:
        parts.append(f"BAD REACTION: {review.reaction}")
    if review.notes:
        parts.append(f"notes: {review.notes}")
    return "; ".join(parts)


def build_prompt(request):
    """The user message: everything the model needs to know about the family and this week."""
    today = timezone.localdate()
    members = list(FamilyMember.objects.all())
    names = {m.pk: m for m in members}
    household = Household.load()
    week_end = request.week + timedelta(days=6)
    out = [f"Today is {today:%A %d %B %Y}. Plan the menu for the week of {request.week:%A %d %B} to {week_end:%A %d %B %Y}."]

    out.append("\n## Family")
    out += [_member_line(m, today) for m in members] or ["(No family members entered yet.)"]

    rules = Rule.objects.filter(active=True)
    if rules:
        out.append("\n## Rules")
        out += [f"- {r.text}" for r in rules]

    settings_lines = [
        (label, value)
        for label, value in [
            ("Max cooking time on weekdays", household.weekday_minutes and f"{household.weekday_minutes} min"),
            ("Max cooking time at the weekend", household.weekend_minutes and f"{household.weekend_minutes} min"),
            ("Adventurousness", household.adventurousness and f"{household.adventurousness}/10"),
            ("Favourite cuisines", household.cuisines),
            ("Cooking equipment", household.equipment),
            ("Where we shop", household.shops),
            ("Optimise the shopping for", household.get_priority_display()),
            ("Pantry staples we usually have", household.pantry),
        ]
        if value
    ]
    if settings_lines:
        out.append("\n## Household")
        out += [f"- {label}: {value}" for label, value in settings_lines]

    history = (
        PlannedMeal.objects.filter(date__gte=request.week - timedelta(weeks=8), date__lt=request.week)
        .select_related("dish", "feedback")
        .order_by("-date", "-slot")
    )
    if history:
        out.append("\n## Recent menus (newest first)")
        for meal in history:
            line = f"- {meal.date:%a %d %b} {meal.slot}: {meal.dish.name}"
            if meal.leftovers:
                line += " (leftovers)"
            elif meal.dish.recipe_url:
                line += f" <{meal.dish.recipe_url}>"
            feedback = _feedback_text(getattr(meal, "feedback", None))
            if feedback:
                line += f" - {feedback}"
            out.append(line)

    older = (
        Feedback.objects.filter(meal__date__lt=request.week - timedelta(weeks=8))
        .select_related("meal__dish")
        .order_by("-meal__date")[:40]
    )
    if older:
        out.append("\n## Older feedback")
        out += [f"- {f.meal.dish.name} ({f.meal.date:%b %Y}): {_feedback_text(f)}" for f in older]

    existing = PlannedMeal.objects.filter(date__range=(request.week, week_end)).select_related("dish")
    requested = {(s["date"], s["slot"]) for s in request.slots}
    kept = [m for m in existing if request.keep_existing or (m.date.isoformat(), m.slot) not in requested]
    if kept:
        out.append("\n## Already planned this week (keep)")
        out += [f"- {m.date:%a %d %b} {m.slot}: {m.dish.name}{' (leftovers)' if m.leftovers else ''}" for m in kept]

    out.append("\n## Meals to plan")
    taken = {(m.date.isoformat(), m.slot) for m in kept}
    for slot in to_plan(request, taken):
        day = date.fromisoformat(slot["date"])
        who = [names[i] for i in slot["eaters"] if i in names]
        people = ", ".join(f"{m.name} ({m.get_kind_display().lower()})" for m in who) or "the family"
        out.append(f"- {day:%A} {slot['date']} {slot['slot']}: {len(who) or 'all'} eating - {people}")

    if request.details.strip():
        out.append("\n## Notes for this week")
        out.append(request.details.strip())
    return "\n".join(out)


def to_plan(request, taken=frozenset()):
    """The requested slots that still need a meal."""
    return [s for s in request.slots if (s["date"], s["slot"]) not in taken]


# --- calling the model ---------------------------------------------------------


def ask_model(prompt):
    """Runs the request until the model calls save_menu. Returns (function arguments, usage totals)."""
    client = get_client()
    usage = {"input": 0, "output": 0}
    previous_id = None
    next_input = [{"role": "user", "content": prompt}]
    for _ in range(MAX_TURNS):
        # Web searches run on OpenAI's side within this one call; the model then calls save_menu.
        response = client.responses.create(
            model=settings.AI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=next_input,
            tools=[web_search_tool(), SAVE_MENU_TOOL],
            tool_choice="auto",
            reasoning={"effort": settings.AI_EFFORT},
            max_output_tokens=64000,
            **({"previous_response_id": previous_id} if previous_id else {}),
        )
        if response.usage:
            usage["input"] += response.usage.input_tokens or 0
            usage["output"] += response.usage.output_tokens or 0

        call = next((i for i in response.output if i.type == "function_call" and i.name == "save_menu"), None)
        if call is not None:
            try:
                return json.loads(call.arguments), usage
            except json.JSONDecodeError:
                raise PlanningError("The AI returned a menu that couldn't be read. Please try again.")
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", "")
            if reason == "max_output_tokens":
                raise PlanningError("The menu was too long to finish. Try planning fewer meals at once.")
            raise PlanningError("The AI stopped before finishing the menu. Please try again.")
        refused = any(
            part.type == "refusal"
            for item in response.output if item.type == "message"
            for part in item.content
        )
        if refused:
            raise PlanningError("The AI declined to plan this menu. Try rewording the notes for the week.")
        # Answered in text instead of saving: continue the same conversation with a reminder.
        previous_id = response.id
        next_input = [{"role": "user", "content": "Please save the menu now by calling save_menu with all meals."}]
    raise PlanningError("The AI didn't return a menu. Please try again.")


# --- saving ------------------------------------------------------------------------


def _decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value)).quantize(Decimal("0.01"))
    except ArithmeticError:
        return None
    return number if number > 0 else None


@transaction.atomic
def save_menu(request, data):
    """Stores the AI's menu. Only requested slots are filled; returns the number of meals saved."""
    week_end = request.week + timedelta(days=6)
    existing = PlannedMeal.objects.filter(date__range=(request.week, week_end))
    if request.keep_existing:
        taken = {(m.date.isoformat(), m.slot) for m in existing}
    else:
        # Replace what was planned in the requested slots.
        for slot in request.slots:
            existing.filter(date=slot["date"], slot=slot["slot"]).delete()
        taken = {(m.date.isoformat(), m.slot) for m in existing.all()}
    wanted = {(s["date"], s["slot"]): s for s in to_plan(request, taken)}
    members = {m.pk: m for m in FamilyMember.objects.all()}

    saved = 0
    for item in data.get("meals", []):
        key = (item.get("date"), item.get("slot"))
        slot = wanted.pop(key, None)
        name = " ".join(str(item.get("dish_name", "")).split())[:200]
        if slot is None or not name:
            logger.warning("Skipping meal outside the request: %s %s", key, name)
            continue
        leftovers = bool(item.get("leftovers"))
        dish = Dish.objects.filter(name__iexact=name).first()
        is_new = dish is None
        if is_new:
            dish = Dish(name=name)
        if item.get("kind") in KINDS and (is_new or dish.kind == Dish.Kind.OTHER):
            dish.kind = item["kind"]
        if item.get("minutes") and not leftovers and (is_new or not dish.minutes):
            dish.minutes = max(0, min(int(item["minutes"]), 1000))
        url = str(item.get("recipe_url", "")).strip()
        if url.startswith(("http://", "https://")) and (is_new or not dish.recipe_url):
            dish.recipe_url = url[:500]
        servings = max(0, min(int(item.get("servings") or 0), 100))
        ingredients = [] if leftovers else item.get("ingredients", [])
        has_ingredients = dish.pk is not None and dish.ingredients.exists()
        if ingredients and not has_ingredients:
            # Keep ingredients the family already entered; new ones are for the portions the AI planned.
            dish.servings = servings or dish.servings
            dish.save()
            Ingredient.objects.bulk_create(
                Ingredient(
                    dish=dish,
                    name=" ".join(str(ing.get("name", "")).split())[:100],
                    quantity=_decimal(ing.get("quantity")),
                    unit=str(ing.get("unit", ""))[:20],
                    category=ing.get("category") if ing.get("category") in CATEGORIES else Category.OTHER,
                    note=str(ing.get("note", ""))[:200],
                )
                for ing in ingredients
                if str(ing.get("name", "")).strip()
            )
        else:
            dish.save()
        meal = PlannedMeal.objects.create(
            date=item["date"],
            slot=item["slot"],
            dish=dish,
            leftovers=leftovers,
            note=str(item.get("note", ""))[:300],
            servings=None if leftovers else (servings or None),
            updated_by=request.created_by,
        )
        meal.eaters.set([members[i] for i in slot["eaters"] if i in members])
        saved += 1
    if wanted:
        logger.warning("The AI left %d requested meals unplanned: %s", len(wanted), sorted(wanted))
    return saved


# --- running in the background ------------------------------------------------------


def run(request_id):
    """Creates the menu for a MenuRequest. Safe to run in a thread."""
    try:
        request = MenuRequest.objects.select_related("created_by").get(pk=request_id)
        request.status = MenuRequest.Status.RUNNING
        request.save(update_fields=["status"])
        try:
            data, usage = ask_model(build_prompt(request))
            request.input_tokens, request.output_tokens = usage["input"], usage["output"]
            saved = save_menu(request, data)
            if not saved:
                raise PlanningError("The AI's menu didn't match the meals you asked for. Please try again.")
            request.summary = str(data.get("summary", "")).strip()
            request.status = MenuRequest.Status.DONE
        except PlanningError as exc:
            request.status, request.error = MenuRequest.Status.FAILED, str(exc)
        except Exception as exc:  # network, API or unexpected errors: keep the app usable
            logger.exception("Menu request %s failed", request_id)
            request.status = MenuRequest.Status.FAILED
            request.error = f"Something went wrong while creating the menu ({exc.__class__.__name__}). Please try again."
        request.finished_at = timezone.now()
        request.save()
    finally:
        close_old_connections()


def start(request):
    """Runs the request in a background thread so the page can show progress."""
    threading.Thread(target=run, args=(request.pk,), daemon=True, name=f"menu-request-{request.pk}").start()


def expire_stale():
    """Requests that never finished (e.g. the container restarted) are marked failed."""
    MenuRequest.objects.filter(
        status__in=[MenuRequest.Status.PENDING, MenuRequest.Status.RUNNING],
        created_at__lt=timezone.now() - STALE_AFTER,
    ).update(status=MenuRequest.Status.FAILED, error="This took too long and was stopped. Please try again.",
             finished_at=timezone.now())
