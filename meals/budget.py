"""
What the AI costs, and the daily limits that keep it affordable.

Every call to the AI is written to the AIUsage ledger with its cost. Before a menu or a recipe
is started, the household's spending today is checked against its limit (AI_DAILY_LIMIT_USD,
or the household's own), and everyone's against AI_GLOBAL_DAILY_LIMIT_USD. A request that
has started is allowed to finish, so a day can end slightly above a limit.
"""
from datetime import datetime, time
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import AIUsage


def today_start():
    return timezone.make_aware(datetime.combine(timezone.localdate(), time.min))


def month_start():
    return timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time.min))


def spent(since=None, **filters):
    """Total cost in USD, e.g. spent(today_start(), household=h)."""
    usage = AIUsage.objects.filter(**filters)
    if since:
        usage = usage.filter(created_at__gte=since)
    return usage.aggregate(total=Sum("cost"))["total"] or Decimal(0)


def daily_limit(household):
    return household.ai_daily_limit if household.ai_daily_limit is not None else settings.AI_DAILY_LIMIT_USD


def blocked(household):
    """Why the household can't use AI right now, or "" if it can."""
    if not settings.OPENAI_API_KEY:
        return "AI isn't set up yet (OPENAI_API_KEY is missing)."
    if not household.ai_approved:
        return "AI features are switched on once an admin has approved your household. Everything else already works."
    limit = daily_limit(household)
    if limit <= 0:
        return "AI features are switched off for your household."
    if spent(today_start(), household=household) >= limit:
        return f"Your household has used today's AI budget (${limit:.2f}). It starts again tomorrow."
    if spent(today_start()) >= settings.AI_GLOBAL_DAILY_LIMIT_USD:
        return "The app has used its AI budget for today. Please try again tomorrow."
    return ""


def record(job, kind, model, usage):
    """Writes one AI call to the ledger. job: the MenuRequest or RecipeImport it was for;
    usage: the response's usage (tokens), plus the number of web searches."""
    from .planner import cost

    return AIUsage.objects.create(
        household_id=job.household_id, user_id=job.created_by_id, kind=kind, model=model,
        input_tokens=usage["input"], output_tokens=usage["output"], web_searches=usage["searches"],
        cost=cost(model, usage) or 0,
    )

