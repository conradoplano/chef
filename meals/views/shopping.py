"""The week's shopping list: ticking items off (synced between phones) and extra items."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import shopping as shopping_list
from ..forms import ExtraItemForm
from ..models import ExtraItem, ShoppingCheck
from .common import parse_date, week_context, week_start


@login_required
def shopping(request, day=None):
    context = week_context(request, day)
    sections, at_home, missing = shopping_list.build(context["start"])
    items = [*at_home, *(i for s in sections for i in s.items)]
    context.update(
        sections=sections,
        at_home=at_home,
        missing=missing,
        total=len(items),
        bought=sum(i.checked for i in items),
        signature=shopping_list.signature(sections, at_home),
        extra_form=ExtraItemForm(prefix="extra"),
        removed=ExtraItem.objects.filter(
            pk=request.GET.get("removed") if request.GET.get("removed", "").isdigit() else None,
            removed_at__isnull=False,
        ).first(),
    )
    return render(request, "meals/shopping.html", context)


@login_required
def shopping_state(request, day):
    return JsonResponse(shopping_list.state(week_start(parse_date(day))))


@login_required
@require_POST
def shopping_toggle(request, day):
    """Marks one item as bought or not. Sends the wanted state rather than flipping,
    so two people tapping at the same time don't undo each other."""
    week = week_start(parse_date(day))
    key = request.POST.get("key", "")[:120]
    if key:
        if request.POST.get("checked") == "1":
            ShoppingCheck.objects.update_or_create(week=week, key=key, defaults={"checked_by": request.user})
        else:
            ShoppingCheck.objects.filter(week=week, key=key).delete()
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse(shopping_list.state(week))
    return redirect("meals:shopping_of", day=week.isoformat())


@login_required
@require_POST
def extra_add(request, day):
    week = week_start(parse_date(day))
    form = ExtraItemForm(request.POST, prefix="extra")
    if form.is_valid():
        extra = form.save(commit=False)
        extra.week = week
        extra.created_by = request.user
        extra.save()
        messages.success(request, f"Added {extra}.")
    else:
        messages.error(request, "Enter a name for the item.")
    return redirect("meals:shopping_of", day=week.isoformat())


@login_required
@require_POST
def extra_delete(request, pk):
    """Hides the item; it can be brought back with Undo until it is purged a day later."""
    extra = get_object_or_404(ExtraItem, pk=pk, removed_at=None)
    now = timezone.now()
    extra.removed_at = now
    extra.save(update_fields=["removed_at"])
    for old in ExtraItem.objects.filter(removed_at__lt=now - timedelta(days=1)):
        ShoppingCheck.objects.filter(week=old.week, key=f"extra-{old.pk}").delete()
        old.delete()
    url = reverse("meals:shopping_of", args=[extra.week.isoformat()])
    return redirect(f"{url}?{urlencode({'removed': extra.pk})}")


@login_required
@require_POST
def extra_restore(request, pk):
    extra = get_object_or_404(ExtraItem, pk=pk)
    extra.removed_at = None
    extra.save(update_fields=["removed_at"])
    messages.success(request, f"{extra} is back on the list.")
    return redirect("meals:shopping_of", day=extra.week.isoformat())
