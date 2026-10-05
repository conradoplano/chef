"""The admin page: new households waiting for AI, what each household's AI costs, limits and suspensions."""
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import User

from .. import budget
from ..models import AIUsage, Household


def staff_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


@staff_only
def manage(request):
    today, month = budget.today_start(), budget.month_start()
    households = list(
        Household.objects.prefetch_related(Prefetch("members", queryset=User.objects.order_by("date_joined")))
        .annotate(
            cost_today=Sum("ai_usage__cost", filter=Q(ai_usage__created_at__gte=today)),
            cost_month=Sum("ai_usage__cost", filter=Q(ai_usage__created_at__gte=month)),
            cost_total=Sum("ai_usage__cost"),
        )
        .order_by("-created_at")
    )
    for household in households:
        household.limit = budget.daily_limit(household)
        household.over_limit = (household.cost_today or 0) >= household.limit
    by_model = (
        AIUsage.objects.values("model")
        .annotate(calls=Count("pk"), month=Sum("cost", filter=Q(created_at__gte=month)), total=Sum("cost"))
        .order_by("-total")
    )
    return render(request, "meals/manage.html", {
        "pending": [h for h in households if not h.ai_approved and h.is_active],
        "households": households,
        "by_model": by_model,
        "today": budget.spent(today),
        "month": budget.spent(month),
        "total": budget.spent(),
        "global_limit": settings.AI_GLOBAL_DAILY_LIMIT_USD,
        "default_limit": settings.AI_DAILY_LIMIT_USD,
    })


@staff_only
@require_POST
def manage_household(request, pk):
    household = get_object_or_404(Household, pk=pk)
    action = request.POST.get("action")
    if action in ("suspend", "revoke") and household.pk == request.household.pk:
        messages.error(request, "That's your own household.")
    elif action == "approve":
        household.ai_approved = True
        household.save(update_fields=["ai_approved"])
        messages.success(request, f"{household} can use AI now.")
    elif action == "revoke":
        household.ai_approved = False
        household.save(update_fields=["ai_approved"])
        messages.success(request, f"{household} can't use AI any more.")
    elif action == "suspend":
        household.is_active = False
        household.save(update_fields=["is_active"])
        messages.success(request, f"Suspended {household}: its members can't log in.")
    elif action == "reactivate":
        household.is_active = True
        household.save(update_fields=["is_active"])
        messages.success(request, f"{household} can log in again.")
    elif action == "limit":
        value = request.POST.get("limit", "").strip()
        try:
            limit = Decimal(value).quantize(Decimal("0.01")) if value else None
        except InvalidOperation:
            limit = Decimal(-1)
        if limit is not None and not 0 <= limit < 10000:
            messages.error(request, "Enter the limit in dollars, e.g. 0.50 – or leave it empty for the default.")
        else:
            household.ai_daily_limit = limit
            household.save(update_fields=["ai_daily_limit"])
            messages.success(request, f"{household}: AI limit is {'$' + str(limit) if limit is not None else 'the default'} per day.")
    return redirect("meals:manage")
