"""The week's shopping list: ticking items off (synced between phones) and extra items."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import services
from .. import shopping as shopping_list
from ..forms import ExtraItemForm
from ..models import ExtraItem
from .common import parse_date, safe_next, week_context, week_start


@login_required
def shopping(request, day=None):
    context = week_context(request, day)
    sections, at_home, missing = shopping_list.build(request.household, context["start"])
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
            household=request.household,
            pk=request.GET.get("removed") if request.GET.get("removed", "").isdigit() else None,
            removed_at__isnull=False,
        ).first(),
    )
    return render(request, "meals/shopping.html", context)


@login_required
def shopping_state(request, day):
    return JsonResponse(shopping_list.state(request.household, week_start(parse_date(day))))


@login_required
@require_POST
def shopping_toggle(request, day):
    """Marks one item as bought or not. Sends the wanted state rather than flipping,
    so two people tapping at the same time don't undo each other."""
    week = week_start(parse_date(day))
    household = request.household
    key = request.POST.get("key", "")[:120]
    if key:
        services.set_bought(household, request.user, week, key, request.POST.get("checked") == "1")
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse(shopping_list.state(household, week))
    return redirect("meals:shopping_of", day=week.isoformat())


def staple_list(request):
    kind = request.POST.get("list", "pantry")
    return kind if kind in shopping_list.STAPLE_LISTS else "pantry"


@login_required
@require_POST
def staple_add(request):
    """Something we always have (pantry, freezer): from the shopping list or the settings page."""
    kind, name = staple_list(request), " ".join(request.POST.get("name", "").split())
    icon, label = shopping_list.STAPLE_LISTS[kind]
    if shopping_list.add_staple(request.household, kind, name):
        messages.success(request, f"{icon} {name} is on the {label} list now.")
    elif name:
        messages.info(request, f"{name} is already on the {label} list.")
    return redirect(safe_next(request) or reverse("meals:family") + "#staples")


@login_required
@require_POST
def staple_remove(request):
    kind, name = staple_list(request), request.POST.get("name", "")
    if shopping_list.remove_staple(request.household, kind, name):
        messages.success(request, f"Removed {name} from the {shopping_list.STAPLE_LISTS[kind][1]} list.")
    return redirect(safe_next(request) or reverse("meals:family") + "#staples")


@login_required
@require_POST
def extra_add(request, day):
    week = week_start(parse_date(day))
    form = ExtraItemForm(request.POST, prefix="extra")
    if form.is_valid():
        extra = services.add_extra_item(request.household, request.user, week, **form.cleaned_data)
        messages.success(request, f"Added {extra}.")
    else:
        messages.error(request, "Enter a name for the item.")
    return redirect("meals:shopping_of", day=week.isoformat())


@login_required
@require_POST
def extra_delete(request, pk):
    """Hides the item; it can be brought back with Undo until it is purged a day later."""
    extra = get_object_or_404(ExtraItem, pk=pk, household=request.household, removed_at=None)
    services.remove_extra_item(extra)
    url = reverse("meals:shopping_of", args=[extra.week.isoformat()])
    return redirect(f"{url}?{urlencode({'removed': extra.pk})}")


@login_required
@require_POST
def extra_restore(request, pk):
    extra = get_object_or_404(ExtraItem, pk=pk, household=request.household)
    extra.removed_at = None
    extra.save(update_fields=["removed_at"])
    messages.success(request, f"{extra} is back on the list.")
    return redirect("meals:shopping_of", day=extra.week.isoformat())
