from decimal import Decimal

from django.db import migrations

# The prices when this was written (USD per million input / output tokens; per web search), so the
# migration gives the same result whenever it runs, whatever planner.PRICES says later.
PRICES = {
    "gpt-5.4": (Decimal("2.50"), Decimal("15.00")),
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-5.4-nano": (Decimal("0.20"), Decimal("1.25")),
    "gpt-5.4-pro": (Decimal("30.00"), Decimal("180.00")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("50")),
}
WEB_SEARCH_PRICE = Decimal("0.01")


def prices_for(model):
    """Like planner.prices_for: the exact name, or the longest known name of a dated version."""
    model = (model or "").strip().lower()
    if model in PRICES:
        return PRICES[model]
    known = [name for name in PRICES if model.startswith(name + "-")]
    return PRICES[max(known, key=len)] if known else None


def cost(prices, row):
    tokens = (row.input_tokens * prices[0] + row.output_tokens * prices[1]) / Decimal(1_000_000)
    return (tokens + row.web_searches * WEB_SEARCH_PRICE).quantize(Decimal("0.0001"))


def price_missing_costs(apps, schema_editor):
    """AI calls recorded without a cost because their model wasn't in the price list yet (e.g. gpt-5.4).
    Calls without a model name were dealt with by 0020; anything still unknown is left as it is."""
    for name in ["MenuRequest", "RecipeImport"]:
        for job in apps.get_model("meals", name).objects.filter(cost=None, input_tokens__gt=0).exclude(model=""):
            prices = prices_for(job.model)
            if prices:
                job.cost = cost(prices, job)
                job.save(update_fields=["cost"])
    for entry in apps.get_model("meals", "AIUsage").objects.filter(cost=0, input_tokens__gt=0).exclude(model=""):
        prices = prices_for(entry.model)
        if prices:
            entry.cost = cost(prices, entry)
            entry.save(update_fields=["cost"])


class Migration(migrations.Migration):

    dependencies = [
        ("meals", "0021_weekly_skips"),
    ]

    operations = [
        migrations.RunPython(price_missing_costs, migrations.RunPython.noop),
    ]
