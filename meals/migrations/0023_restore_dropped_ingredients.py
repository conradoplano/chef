import re
from decimal import Decimal, InvalidOperation

from django.db import migrations


def key(name):
    """Like shopping.item_key: case, spacing and simple plurals don't matter."""
    name = re.sub(r"\s+", " ", (name or "").strip().lower())
    if name.endswith("oes"):
        return name[:-2]
    return name[:-1] if name.endswith("s") and not name.endswith("ss") else name


def decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def restore_dropped_ingredients(apps, schema_editor):
    """Saving a recipe read by AI left out ingredients that were accepted exactly as read and had no
    quantity ("a handful of mint"). They are added back from what the AI read, unless the recipe already
    has that ingredient (also under a longer or shorter name, e.g. "mint" for "mint leaves")."""
    RecipeImport = apps.get_model("meals", "RecipeImport")
    Ingredient = apps.get_model("meals", "Ingredient")
    added = 0
    for job in RecipeImport.objects.filter(status="saved").exclude(dish=None).select_related("dish"):
        have = [key(name) for name in Ingredient.objects.filter(dish=job.dish).values_list("name", flat=True)]
        for read in (job.result or {}).get("ingredients", []):
            name = " ".join(str(read.get("name", "")).split())[:100]
            if not name or decimal(read.get("quantity")) is not None:
                continue  # rows with a quantity were always saved
            wanted = key(name)
            if any(wanted == k or wanted in k or k in wanted for k in have if k):
                continue
            Ingredient.objects.create(
                dish=job.dish, name=name, quantity=None, unit=str(read.get("unit", ""))[:20],
                category=read.get("category") or "other", note=str(read.get("note", ""))[:200],
            )
            have.append(wanted)
            added += 1
    if added:
        print(f"\n  Added {added} ingredient{'s' if added != 1 else ''} left out of recipes read by AI.")


class Migration(migrations.Migration):

    dependencies = [
        ("meals", "0022_price_missing_ai_costs"),
    ]

    operations = [
        migrations.RunPython(restore_dropped_ingredients, migrations.RunPython.noop),
    ]
