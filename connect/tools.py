"""
The tools an assistant can use through MCP. Each works on the household of the person who connected
the assistant, through the same functions as the web pages (meals/services.py). Results are plain
JSON-like dicts; a ToolError is shown to the assistant as a failed call with its message.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone

from meals import services, shopping
from meals.models import Category, Dish, Feedback, MenuRequest, PlannedMeal
from meals.shopping import item_key

TOOLS = {}
RATINGS = Feedback.Rating.values  # loved, liked, okay, disliked
SLOTS = PlannedMeal.Slot.values  # lunch, dinner
MAX_RECIPES = 30


class ToolError(Exception):
    """Something the assistant should tell the person, or correct and try again."""


@dataclass
class Context:
    user: object
    household: object
    base_url: str


def tool(name, title, description, properties=None, required=(), read_only=False, destructive=False, idempotent=False):
    def register(function):
        TOOLS[name] = {
            "function": function,
            "spec": {
                "name": name,
                "title": title,
                "description": description,
                "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                                "additionalProperties": False},
                "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                                "idempotentHint": idempotent, "openWorldHint": False},
            },
        }
        return function
    return register


# --- arguments -------------------------------------------------------------------------------

WEEK = {"type": "string", "format": "date",
        "description": "Any date (YYYY-MM-DD) in the week; weeks run Monday to Sunday. Default: this week."}


def text(args, name, max_length, required=False):
    value = " ".join(str(args.get(name) or "").split())
    if required and not value:
        raise ToolError(f"{name} is required.")
    if len(value) > max_length:
        raise ToolError(f"{name} can be at most {max_length} characters.")
    return value


def day(args, name, required=False):
    value = args.get(name)
    if not value:
        if required:
            raise ToolError(f"{name} is required (YYYY-MM-DD).")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"{name} must be a date like 2026-10-05.")


def week_of(args, name="week"):
    return services.week_start(day(args, name) or timezone.localdate())


def choice(args, name, choices, default=None):
    value = args.get(name, default)
    if value is not None and value not in choices:
        raise ToolError(f"{name} must be one of: {', '.join(choices)}.")
    return value


def meal_in(context, args):
    meal_id = args.get("meal_id")
    meal = PlannedMeal.objects.filter(household=context.household, pk=meal_id).select_related("dish").first() \
        if isinstance(meal_id, int) else None
    if meal is None:
        raise ToolError(f"There's no meal {meal_id}. get_week_menu lists the meals with their meal_id.")
    return meal


# --- how things are described -----------------------------------------------------------------


def meal_info(meal):
    review = getattr(meal, "feedback", None)
    return {
        "meal_id": meal.pk,
        "date": meal.date.isoformat(),
        "slot": meal.slot,
        "dish": meal.dish.name,
        "recipe_id": meal.dish_id,
        "leftovers": meal.leftovers,
        "minutes": None if meal.leftovers else meal.dish.minutes,
        "portions": meal.servings,
        "eaters": [m.name for m in meal.eaters.all()],
        "note": meal.note,
        "recipe_link": meal.dish.recipe_url or None,
        "feedback": feedback_info(review),
    }


def feedback_info(review):
    if not review:
        return None
    return {"kids": review.kids or None, "parents": review.parents or None,
            "bad_reaction": review.reaction or None, "notes": review.notes or None}


def item_info(item):
    return {
        "item": item.key,
        "name": item.name,
        "quantity": item.quantity,
        "notes": item.notes,
        "for_meals": [use.label for use in item.uses],
        "every_week": bool(item.weekly),
        "added_by_hand": bool(item.extra),
        "bought": item.checked,
        "bought_by": item.checked_by or None,
    }


def recipe_summary(dish):
    return {
        "recipe_id": dish.pk,
        "name": dish.name,
        "type": dish.kind,
        "binder": dish.status or None,
        "minutes": dish.minutes,
        "times_cooked": dish.cooked,
        "last_cooked": dish.last_cooked.isoformat() if dish.last_cooked else None,
        "latest_feedback": feedback_info(dish.latest_feedback),
        "recipe_link": dish.recipe_url or None,
        "source": dish.source or None,
    }


# --- menu --------------------------------------------------------------------------------------


@tool("get_week_menu", "Get the week's menu",
      "The household's planned meals for one week (Monday to Sunday): per day lunch and/or dinner with the dish, "
      "recipe_id, who eats, portions, notes and feedback. Use the meal_id to remove or rate a meal.",
      {"week": WEEK}, read_only=True, idempotent=True)
def get_week_menu(context, args):
    start = week_of(args)
    meals = (
        PlannedMeal.objects.filter(household=context.household, date__range=(start, start + timedelta(days=6)))
        .select_related("dish", "feedback").prefetch_related("eaters")
    )
    by_day = {}
    for meal in meals:
        by_day.setdefault(meal.date, []).append(meal_info(meal))
    about = (
        MenuRequest.objects.filter(household=context.household, week=start, status=MenuRequest.Status.DONE,
                                   kind__in=[MenuRequest.Kind.CREATE, MenuRequest.Kind.CHANGE])
        .exclude(summary="").first()
    )
    days = [start + timedelta(days=i) for i in range(7)]
    return {
        "week_start": start.isoformat(),
        "week_end": days[-1].isoformat(),
        "days": [{"date": d.isoformat(), "weekday": f"{d:%A}", "meals": by_day.get(d, [])} for d in days],
        "about_this_menu": about.summary if about else None,
    }


@tool("plan_meal", "Plan a meal",
      "Puts a dish on the menu for a day's lunch or dinner, for the people who usually eat that meal. `recipe` is "
      "the recipe_id or the exact name of a recipe the household has (see search_recipes); any other name creates "
      "a new dish without ingredients. Set leftovers to true when the meal is eaten from an earlier meal's leftovers "
      "(then nothing is added to the shopping list).",
      {
          "date": {"type": "string", "format": "date", "description": "The day, YYYY-MM-DD."},
          "slot": {"type": "string", "enum": SLOTS},
          "recipe": {"type": "string", "description": "recipe_id, or the name of a recipe or of a new dish."},
          "leftovers": {"type": "boolean", "default": False},
      },
      required=("date", "slot", "recipe"))
def plan_meal(context, args):
    when, slot = day(args, "date", required=True), choice(args, "slot", SLOTS)
    if slot is None:
        raise ToolError("slot is required: lunch or dinner.")
    recipe = text(args, "recipe", 200, required=True)
    dishes = Dish.objects.filter(household=context.household)
    dish = (dishes.filter(pk=int(recipe)).first() if recipe.isdigit() else None) or dishes.filter(name__iexact=recipe).first()
    new = dish is None
    if new:
        dish = Dish.objects.create(household=context.household, name=recipe)
    others = list(PlannedMeal.objects.filter(household=context.household, date=when, slot=slot).select_related("dish"))
    meal = services.plan_dish(dish, when, slot, context.user, leftovers=bool(args.get("leftovers")))
    result = {"planned": meal_info(meal), "new_dish": new}
    if not meal.leftovers and not dish.ingredients.exists():
        result["note"] = "This dish has no ingredients in Chef yet, so it adds nothing to the shopping list."
    if others:
        result["also_planned_for_this_meal"] = [m.dish.name for m in others]
    return result


@tool("remove_meal", "Remove a meal",
      "Takes a meal off the menu (by meal_id from get_week_menu). By default, leftover meals of it later that week "
      "are removed too. The recipe stays in the household's recipes.",
      {"meal_id": {"type": "integer"}, "with_leftovers": {"type": "boolean", "default": True}},
      required=("meal_id",), destructive=True)
def remove_meal(context, args):
    meal = meal_in(context, args)
    info = meal_info(meal)
    leftovers = services.remove_meal(meal, with_leftovers=args.get("with_leftovers", True) is not False)
    return {"removed": info, "leftovers_removed": [f"{m.date:%A} {m.slot}" for m in leftovers]}


@tool("copy_week", "Copy a week's menu",
      "Copies all meals of one week into another, keeping weekday and lunch/dinner. Meals already planned in the "
      "target week stay; those slots are skipped.",
      {"from_week": {**WEEK, "description": "Any date in the week to copy from."},
       "to_week": {**WEEK, "description": "Any date in the week to copy to."}},
      required=("from_week", "to_week"))
def copy_week(context, args):
    source = services.week_start(day(args, "from_week", required=True))
    target = services.week_start(day(args, "to_week", required=True))
    if source == target:
        raise ToolError("from_week and to_week are the same week.")
    copied, skipped = services.copy_week(context.household, context.user, source, target)
    return {"to_week_start": target.isoformat(), "copied": copied, "skipped_already_planned": skipped}


# --- shopping list ----------------------------------------------------------------------------


@tool("get_shopping_list", "Get the shopping list",
      "The week's shopping list, built from the planned meals' ingredients (merged and scaled), items added by "
      "hand and items bought every week, grouped by shop section, with what is already bought. `check_at_home` "
      "are staples the household usually has. Use `item` to tick or remove an item.",
      {"week": WEEK}, read_only=True, idempotent=True)
def get_shopping_list(context, args):
    start = week_of(args)
    sections, at_home, missing = shopping.build(context.household, start)
    return {
        "week_start": start.isoformat(),
        "sections": [{"section": s.label, "items": [item_info(i) for i in s.items]} for s in sections],
        "check_at_home": [item_info(i) for i in at_home],
        "meals_without_ingredients": [f"{m.date:%a} {m.slot}: {m.dish.name}" for m in missing],
    }


def find_item(context, args, start):
    ref = text(args, "item", 120, required=True)
    sections, at_home, _ = shopping.build(context.household, start)
    items = [*at_home, *(i for s in sections for i in s.items)]
    matches = [i for i in items if i.key == ref] or [
        i for i in items if item_key(i.name) == item_key(ref) or i.name.lower() == ref.lower()
    ]
    if not matches:
        raise ToolError(f"“{ref}” isn't on the shopping list for the week of {start:%d %b}. get_shopping_list shows the items.")
    if len(matches) > 1:
        raise ToolError("Several items match: " + ", ".join(f"{i.name} (item {i.key})" for i in matches) + ". Use the item key.")
    return matches[0]


@tool("add_item", "Add to the shopping list",
      "Adds something that isn't for a planned meal (e.g. milk, batteries) to the week's shopping list.",
      {
          "name": {"type": "string", "maxLength": 100},
          "quantity": {"type": "string", "maxLength": 50, "description": "E.g. 2 l, 500 g, 3."},
          "category": {"type": "string", "enum": Category.values, "default": "other",
                       "description": "Shop section: " + ", ".join(f"{v} ({label.split(' ', 1)[1]})" for v, label in Category.choices)},
          "note": {"type": "string", "maxLength": 200},
          "week": WEEK,
      },
      required=("name",))
def add_item(context, args):
    start = week_of(args)
    extra = services.add_extra_item(
        context.household, context.user, start, name=text(args, "name", 100, required=True),
        quantity=text(args, "quantity", 50), category=choice(args, "category", Category.values, "other"),
        note=text(args, "note", 200),
    )
    return {"added": {"item": f"extra-{extra.pk}", "name": extra.name, "quantity": extra.quantity,
                      "category": extra.category}, "week_start": start.isoformat()}


@tool("remove_item", "Remove from the shopping list",
      "Removes an item that was added by hand (add_item or in the app), or takes an every-week item off this week's "
      "list only (it's back the week after). Items for planned meals can't be removed on their own: tick them as "
      "bought instead, or remove the meal.",
      {"item": {"type": "string", "description": "The item key from get_shopping_list, or the item's name."}, "week": WEEK},
      required=("item",), destructive=True)
def remove_item(context, args):
    start = week_of(args)
    item = find_item(context, args, start)
    if item.weekly:
        services.skip_weekly_item(item.weekly, start)
        return {"removed": item.name, "week_start": start.isoformat(), "only_this_week": True}
    if not item.extra:
        raise ToolError(f"{item.name} is needed for " + ", ".join(u.label for u in item.uses)
                        + ". Tick it as bought instead, or remove the meal.")
    services.remove_extra_item(item.extra)
    return {"removed": item.name, "week_start": start.isoformat()}


@tool("tick_item", "Tick off a shopping item",
      "Marks an item on the week's shopping list as bought (or not bought again with bought=false). Everyone in the "
      "household sees it on their phone.",
      {"item": {"type": "string", "description": "The item key from get_shopping_list, or the item's name."},
       "bought": {"type": "boolean", "default": True}, "week": WEEK},
      required=("item",), idempotent=True)
def tick_item(context, args):
    start = week_of(args)
    item = find_item(context, args, start)
    bought = args.get("bought", True) is not False
    services.set_bought(context.household, context.user, start, item.key, bought)
    return {"item": item.key, "name": item.name, "bought": bought}


# --- recipes ------------------------------------------------------------------------------------


@tool("search_recipes", "Search recipes",
      "Finds the household's recipes by words in the name, ingredients, notes, source or type (all words must "
      "match; empty finds all). Favourites first, then recipes they want to try, then other dishes they cooked.",
      {"query": {"type": "string"},
       "binder": {"type": "string", "enum": ["all", "favourites", "try"], "default": "all",
                  "description": "favourites, recipes to try, or all dishes."}},
      read_only=True, idempotent=True)
def search_recipes(context, args):
    binder = choice(args, "binder", ["all", "favourites", "try"], "all")
    dishes = Dish.objects.filter(household=context.household)
    if binder != "all":
        dishes = dishes.filter(status=Dish.Status.FAVOURITE if binder == "favourites" else Dish.Status.TRY)
    found = services.search_recipes(context.household, text(args, "query", 200), dishes)
    order = {Dish.Status.FAVOURITE: 0, Dish.Status.TRY: 1}
    found.sort(key=lambda d: (order.get(d.status, 2), -(d.last_cooked.toordinal() if d.last_cooked else 0), d.name.lower()))
    result = {"count": len(found), "recipes": [recipe_summary(d) for d in found[:MAX_RECIPES]]}
    if len(found) > MAX_RECIPES:
        result["more"] = f"{len(found) - MAX_RECIPES} more; search with more words to narrow it down."
    return result


@tool("get_recipe", "Get a recipe",
      "One recipe with ingredients (for `portions` portions), method, notes, link or source, and when it was cooked "
      "with the family's feedback.",
      {"recipe_id": {"type": "integer"}}, required=("recipe_id",), read_only=True, idempotent=True)
def get_recipe(context, args):
    recipe_id = args.get("recipe_id")
    dishes = Dish.objects.filter(household=context.household, pk=recipe_id) if isinstance(recipe_id, int) else Dish.objects.none()
    found = services.with_history(dishes)
    if not found:
        raise ToolError(f"There's no recipe {recipe_id}. search_recipes lists the recipes with their recipe_id.")
    dish = found[0]
    history = PlannedMeal.objects.filter(dish=dish, leftovers=False).select_related("feedback").order_by("-date")[:10]
    return {
        **recipe_summary(dish),
        "portions": dish.servings,
        "notes": dish.notes or None,
        "ingredients": [
            {"name": i.name, "quantity": float(i.quantity) if i.quantity is not None else None, "unit": i.unit,
             "section": i.category, "note": i.note or None}
            for i in dish.ingredients.all()
        ],
        "method": dish.steps,
        "photos": len(dish.photos.all()),
        "cooked": [{"date": m.date.isoformat(), "feedback": feedback_info(getattr(m, "feedback", None))} for m in history],
        "open_in_chef": context.base_url + reverse("meals:recipe", args=[dish.pk]),
    }


# --- feedback -------------------------------------------------------------------------------------


@tool("rate_meal", "Rate a meal",
      "Records how a meal that was eaten went: the kids' and parents' rating, a bad reaction, notes. Given values "
      "replace earlier ones; the rest is kept. A recipe to try that everyone loved or liked becomes a favourite. "
      "Leftover meals share the feedback of the meal they came from: rate that one.",
      {
          "meal_id": {"type": "integer"},
          "kids": {"type": "string", "enum": RATINGS},
          "parents": {"type": "string", "enum": RATINGS},
          "notes": {"type": "string", "maxLength": 2000, "description": "E.g. too spicy for the kids."},
          "bad_reaction": {"type": "string", "maxLength": 300, "description": "Who reacted badly to it, and how."},
      },
      required=("meal_id",))
def rate_meal(context, args):
    meal = meal_in(context, args)
    if meal.leftovers:
        cooked = PlannedMeal.objects.filter(household=context.household, dish=meal.dish, leftovers=False,
                                            date__lte=meal.date).order_by("-date").first()
        raise ToolError("That meal was leftovers" + (f"; rate meal {cooked.pk} ({cooked.date:%A}) instead." if cooked else "."))
    if meal.date > timezone.localdate():
        raise ToolError(f"That meal is on {meal.date:%A %d %b}; it can be rated once it has been eaten.")
    kids, parents = choice(args, "kids", RATINGS), choice(args, "parents", RATINGS)
    notes = text(args, "notes", 2000) if "notes" in args else None
    reaction = text(args, "bad_reaction", 300) if "bad_reaction" in args else None
    if kids is None and parents is None and notes is None and reaction is None:
        raise ToolError("Give at least one of kids, parents, notes or bad_reaction.")
    current = Feedback.objects.filter(meal=meal).first()
    review, promoted = services.save_feedback(
        meal, context.user,
        kids=kids if kids is not None else (current.kids if current else ""),
        parents=parents if parents is not None else (current.parents if current else ""),
        reaction=reaction, notes=notes,
    )
    return {"meal_id": meal.pk, "dish": meal.dish.name, "feedback": feedback_info(review),
            "became_favourite": promoted}


def call(name, context, args):
    """Runs a tool. Raises KeyError for unknown tools and ToolError for bad input."""
    return TOOLS[name]["function"](context, args)

