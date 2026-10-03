"""
Builds a week's shopping list from the planned meals.

Ingredients with the same name are merged across dishes; quantities in the same
unit are added up (g/kg and ml/l are converted). Every line remembers which
meals it is for. Leftover meals add nothing.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from .models import Category, ExtraItem, Household, PlannedMeal, ShoppingCheck

# Units that are converted to a base unit before adding up: unit -> (base, factor).
CONVERSIONS = {
    "g": ("g", 1), "gram": ("g", 1), "grams": ("g", 1),
    "kg": ("g", 1000),
    "ml": ("ml", 1), "l": ("ml", 1000), "litre": ("ml", 1000), "litres": ("ml", 1000), "liter": ("ml", 1000),
}
# Shown in the larger unit from this amount on.
LARGER = {"g": ("kg", 1000), "ml": ("l", 1000)}
# Units that can be bought in any amount; everything else (pcs, cans, bunches...) is rounded up.
MEASURED = {"g", "ml", "tbsp", "tsp", "cup", "cups", "pinch"}


def item_key(name):
    """Identifies an ingredient across recipes: case, spacing and simple plurals don't matter."""
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key.endswith("oes"):
        key = key[:-2]
    elif key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]
    return key


def format_number(value):
    text = f"{value.normalize():f}" if isinstance(value, Decimal) else str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_quantities(amounts, unknown):
    """amounts: {unit: Decimal}; unknown: whether some recipe didn't give a quantity."""
    parts = []
    for unit, value in amounts.items():
        if unit.lower() not in MEASURED:
            value = value.to_integral_value(rounding=ROUND_CEILING)
        else:
            value = value.quantize(Decimal("1") if value >= 10 else Decimal("0.1"), rounding=ROUND_HALF_UP)
        if unit in LARGER and value >= LARGER[unit][1]:
            unit, value = LARGER[unit][0], value / LARGER[unit][1]
        parts.append(f"{format_number(value)} {unit}".strip())
    if unknown and parts:
        parts.append("more")
    return " + ".join(parts)


@dataclass
class Use:
    meal: PlannedMeal

    @property
    def label(self):
        return f"{self.meal.date:%a} · {self.meal.dish.name}"


@dataclass
class Item:
    key: str
    name: str
    category: str
    quantity: str = ""
    notes: list = field(default_factory=list)
    uses: list = field(default_factory=list)
    extra: ExtraItem = None
    checked_by: str = ""
    checked: bool = False


@dataclass
class Section:
    category: str
    label: str
    items: list


def pantry_keys():
    staples = re.split(r"[\n,;]+", Household.load().pantry)
    return {item_key(s) for s in staples if s.strip()}


def build(week):
    """Returns (sections, at_home, missing) for the week starting on Monday `week`.

    at_home: items that are pantry staples, to check before shopping.
    missing: cooked meals whose dish has no ingredients yet.
    """
    meals = (
        PlannedMeal.objects.filter(date__range=(week, week + timedelta(days=6)), leftovers=False)
        .select_related("dish")
        .prefetch_related("dish__ingredients")
    )
    merged = {}
    amounts = {}
    unknown = set()
    missing = []
    for meal in meals:
        ingredients = meal.dish.ingredients.all()
        if not ingredients:
            missing.append(meal)
        # Quantities are for dish.servings portions; scale to the portions planned for this meal.
        factor = Decimal(meal.servings) / Decimal(meal.dish.servings) if meal.servings and meal.dish.servings else 1
        for ing in ingredients:
            key = item_key(ing.name)
            item = merged.get(key)
            if item is None:
                item = merged[key] = Item(key=key, name=ing.name.strip(), category=ing.category)
                amounts[key] = {}
            elif item.category == Category.OTHER:
                item.category = ing.category
            if ing.quantity is None:
                unknown.add(key)
            else:
                unit = ing.unit.strip().lower()
                base, unit_factor = CONVERSIONS.get(unit, (ing.unit.strip(), 1))
                amounts[key][base] = amounts[key].get(base, Decimal(0)) + ing.quantity * unit_factor * factor
            if ing.note and ing.note not in item.notes:
                item.notes.append(ing.note)
            if not any(use.meal.pk == meal.pk for use in item.uses):
                item.uses.append(Use(meal))
    for key, item in merged.items():
        item.quantity = format_quantities(amounts[key], key in unknown)

    items = list(merged.values())
    for extra in ExtraItem.objects.filter(week=week, removed_at=None):
        items.append(
            Item(key=f"extra-{extra.pk}", name=extra.name, category=extra.category, quantity=extra.quantity,
                 notes=[extra.note] if extra.note else [], extra=extra)
        )

    checks = {c.key: c for c in ShoppingCheck.objects.filter(week=week).select_related("checked_by")}
    for item in items:
        check = checks.get(item.key)
        if check:
            item.checked = True
            item.checked_by = check.checked_by.get_short_name() if check.checked_by else ""

    staples = pantry_keys()
    at_home = sorted((i for i in items if i.key in staples and not i.extra), key=lambda i: i.name.lower())
    at_home_keys = {i.key for i in at_home}
    to_buy = [i for i in items if i.key not in at_home_keys]

    labels = dict(Category.choices)
    sections = []
    for category in Category.values:
        in_section = sorted((i for i in to_buy if i.category == category), key=lambda i: i.name.lower())
        if in_section:
            sections.append(Section(category, labels[category], in_section))
    return sections, at_home, missing


def signature(sections, at_home):
    """Changes when items are added, removed or change quantity, so open pages know to reload."""
    lines = sorted(
        f"{i.key}|{i.quantity}|{len(i.uses)}" for i in [*at_home, *(i for s in sections for i in s.items)]
    )
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]


def state(week):
    """What other devices need to stay in sync: who checked what, and the list's signature."""
    sections, at_home, _ = build(week)
    items = [*at_home, *(i for s in sections for i in s.items)]
    return {
        "checked": {i.key: i.checked_by for i in items if i.checked},
        "signature": signature(sections, at_home),
    }
