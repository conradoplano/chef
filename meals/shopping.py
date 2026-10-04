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
    staple_list: str = ""  # "pantry" or "freezer" if it's a staple we usually have
    staple_name: str = ""  # the staple it matched, e.g. "olive oil" for "Extra virgin olive oil"

    @property
    def can_be_staple(self):
        """The list this item could be kept on (pantry, freezer), if any; fresh food can't."""
        return "" if self.extra else STAPLE_CATEGORIES.get(self.category, "")


@dataclass
class Section:
    category: str
    label: str
    items: list


# Things we usually have at home, kept in lists on the household: list -> (icon, name).
STAPLE_LISTS = {"pantry": ("🫙", "pantry"), "freezer": ("❄️", "freezer")}
# Shopping list sections whose items can be staples, and the list they go on. Fresh food can't.
STAPLE_CATEGORIES = {Category.PANTRY: "pantry", Category.SPECIALITY: "pantry", Category.FROZEN: "freezer"}
# Sections a staple can match on the list: those, plus items without a section.
STAPLE_MATCHES = {
    kind: {c for c, k in STAPLE_CATEGORIES.items() if k == kind} | {Category.OTHER} for kind in STAPLE_LISTS
}


def staple_items(kind):
    """A staples list, as entered (one per line; commas also work)."""
    text = getattr(Household.load(), kind)
    return [" ".join(s.split()) for s in re.split(r"[\n,;]+", text) if s.strip()]


def save_staples(kind, items):
    household = Household.load()
    setattr(household, kind, "\n".join(items))
    household.save(update_fields=[kind])


def add_staple(kind, name):
    """Adds a staple unless it's already on the list (as the same ingredient). Returns True if added."""
    name = " ".join(name.split())[:100]
    items = staple_items(kind)
    if not name or item_key(name) in {item_key(i) for i in items}:
        return False
    save_staples(kind, sorted([*items, name], key=str.lower))
    return True


def remove_staple(kind, name):
    """Removes the staple and anything that is the same ingredient (e.g. "Onions" for "onion")."""
    key = item_key(name)
    items = staple_items(kind)
    kept = [i for i in items if item_key(i) != key]
    if len(kept) != len(items):
        save_staples(kind, kept)
        return True
    return False


def matching_staple(key, staples):
    """The staple that is the item or part of its name, as whole words, or None:
    "olive oil" matches "extra virgin olive oil", but "oil" doesn't match "foil"."""
    return next((name for s, name in staples if key == s or re.search(rf"\b{re.escape(s)}\b", key)), None)


def build(week):
    """Returns (sections, at_home, missing) for the week starting on Monday `week`.

    at_home: items that are staples (pantry or freezer), to check before shopping.
    missing: cooked meals whose dish has no ingredients yet.
    """
    meals = (
        PlannedMeal.objects.filter(date__range=(week, week + timedelta(days=6)), leftovers=False)
        .select_related("dish")
        .prefetch_related("dish__ingredients", "dish__photos")
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

    staples = {kind: [(item_key(s), s) for s in staple_items(kind)] for kind in STAPLE_LISTS}
    for item in items:
        if item.extra:
            continue
        for kind, names in staples.items():
            if item.category in STAPLE_MATCHES[kind]:
                match = matching_staple(item.key, names)
            else:
                # Fresh food only by its exact name: "garlic" can be a staple, but the spice "pepper"
                # mustn't take fresh peppers off the list.
                match = next((name for _, name in names if name.lower() == " ".join(item.name.lower().split())), None)
            if match:
                item.staple_list, item.staple_name = kind, match
                break
    at_home = sorted((i for i in items if i.staple_list), key=lambda i: i.name.lower())
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
