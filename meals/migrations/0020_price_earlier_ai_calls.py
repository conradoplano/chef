from datetime import datetime, timezone
from decimal import Decimal

from django.db import migrations

# The prices when this was written (USD per million input / output tokens; per web search).
PRICES = {
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("50")),
}
WEB_SEARCH_PRICE = Decimal("0.01")
# Requests before cost tracking didn't store their model. Until this commit the default model was
# gpt-6.1-sol, then gpt-5.4-mini.
MINI_SINCE = datetime(2026, 10, 4, 6, 27, 59, tzinfo=timezone.utc)


def model_of(model, created_at):
    model = (model or "").strip().lower()
    if not model:
        return "gpt-6.1-sol" if created_at < MINI_SINCE else "gpt-5.4-mini"
    if model in PRICES:
        return model
    known = [name for name in PRICES if model.startswith(name + "-")]
    return max(known, key=len) if known else None


def cost(model, row):
    prices = PRICES[model]
    tokens = (row.input_tokens * prices[0] + row.output_tokens * prices[1]) / Decimal(1_000_000)
    return (tokens + row.web_searches * WEB_SEARCH_PRICE).quantize(Decimal("0.0001"))


def price_earlier_calls(apps, schema_editor):
    """Calls recorded at $0 because their model wasn't stored or not found in the price list."""
    for name in ["MenuRequest", "RecipeImport"]:
        for job in apps.get_model("meals", name).objects.filter(cost=None, input_tokens__gt=0):
            model = model_of(job.model, job.created_at)
            if model:
                job.model, job.cost = job.model or model, cost(model, job)
                job.save(update_fields=["model", "cost"])
    for entry in apps.get_model("meals", "AIUsage").objects.filter(cost=0, input_tokens__gt=0):
        model = model_of(entry.model, entry.created_at)
        if model:
            entry.model, entry.cost = entry.model or model, cost(model, entry)
            entry.save(update_fields=["model", "cost"])


class Migration(migrations.Migration):

    dependencies = [
        ("meals", "0019_ai_usage"),
    ]

    operations = [
        migrations.RunPython(price_earlier_calls, migrations.RunPython.noop),
    ]
