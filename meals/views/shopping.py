"""The week's shopping list - or a shopping trip's, from today until a chosen day: ticking items off
(synced between phones) and extra items."""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import services
from .. import shopping as shopping_list
from ..forms import ExtraItemForm
from ..models import ExtraItem, WeeklyItem
from .common import parse_date, safe_next, week_context, week_start


TRIP_SESSION_KEY = "shop_until"
TRIP_DAYS = 14  # how far ahead a trip can go


def trip_until(request, day, today):
    """The last day of the shopping trip being shown, or None for the week's list. ?until=<date> starts a
    trip from today (remembered for the rest of the day, like the selected week); ?until= or opening a
    week ends it."""
    if "until" in request.GET:
        try:
            until = date.fromisoformat(request.GET["until"])
        except ValueError:
            until = None
        if until and today <= until < today + timedelta(days=TRIP_DAYS):
            request.session[TRIP_SESSION_KEY] = {"until": until.isoformat(), "on": today.isoformat()}
            return until
        request.session.pop(TRIP_SESSION_KEY, None)
        return None
    if day:
        request.session.pop(TRIP_SESSION_KEY, None)
        return None
    saved = request.session.get(TRIP_SESSION_KEY) or {}
    if saved.get("on") == today.isoformat():
        return date.fromisoformat(saved["until"])
    return None


def trip_dates(start, until):
    """The trip's first and last day from the URL; 404 unless it's a real trip."""
    start, until = parse_date(start), parse_date(until)
    if not start <= until < start + timedelta(days=TRIP_DAYS):
        raise Http404("Not a shopping trip")
    return start, until


@login_required
def shopping(request, day=None):
    today = timezone.localdate()
    until = trip_until(request, day, today)
    if until:
        context = {"start": today, "end": until, "this_week": week_start(today), "today": today, "trip": True}
        week = week_start(today)
        context.update(
            list_url=reverse("meals:shopping") + "?" + urlencode({"until": until.isoformat()}),
            state_url=reverse("meals:shopping_trip_state", args=[today.isoformat(), until.isoformat()]),
            toggle_url=reverse("meals:shopping_trip_toggle", args=[today.isoformat(), until.isoformat()]),
        )
    else:
        context = week_context(request, day)
        week = context["start"]
        context.update(
            list_url=reverse("meals:shopping_of", args=[week.isoformat()]),
            state_url=reverse("meals:shopping_state", args=[week.isoformat()]),
            toggle_url=reverse("meals:shopping_toggle", args=[week.isoformat()]),
        )
    sections, at_home, missing = shopping_list.build(request.household, context["start"], until)
    items = [*at_home, *(i for s in sections for i in s.items)]
    context.update(
        week=week,
        until=until,
        # "Shop until": a trip from today until one of the next days.
        trip_days=[(today + timedelta(days=i), "Today" if i == 0 else "Tomorrow" if i == 1 else None) for i in range(TRIP_DAYS)],
        sections=sections,
        at_home=at_home,
        missing=missing,
        total=len(items),
        bought=sum(i.checked for i in items),
        signature=shopping_list.signature(sections, at_home),
        extra_form=ExtraItemForm(prefix="extra"),
        add=request.GET.get("add", ""),  # a section's "+ Add item" form, opened without JavaScript
        removed=ExtraItem.objects.filter(
            household=request.household,
            pk=request.GET.get("removed") if request.GET.get("removed", "").isdigit() else None,
            removed_at__isnull=False,
        ).first(),
        skipped=WeeklyItem.objects.filter(
            household=request.household,
            pk=request.GET.get("skipped") if request.GET.get("skipped", "").isdigit() else None,
            skips__week=week,
        ).first(),
    )
    return render(request, "meals/shopping.html", context)


@login_required
def shopping_state(request, day):
    return JsonResponse(shopping_list.state(request.household, week_start(parse_date(day))))


@login_required
def shopping_trip_state(request, start, until):
    start, until = trip_dates(start, until)
    return JsonResponse(shopping_list.state(request.household, start, until))


@login_required
@require_POST
def shopping_trip_toggle(request, start, until):
    """Like shopping_toggle, for a trip's own ticks."""
    start, until = trip_dates(start, until)
    key = request.POST.get("key", "")[:120]
    if key:
        services.set_bought(request.household, request.user, start, key, request.POST.get("checked") == "1", trip=True)
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse(shopping_list.state(request.household, start, until))
    return redirect(reverse("meals:shopping") + "?" + urlencode({"until": until.isoformat()}))


def back_to_list(request, week, **params):
    """Back to the list the change was made on (?next=, e.g. a trip), or else the week's list."""
    url = safe_next(request) or reverse("meals:shopping_of", args=[week.isoformat()])
    return redirect(url + (("&" if "?" in url else "?") + urlencode(params) if params else ""))


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
    return back_to_list(request, week)


@login_required
@require_POST
def extra_delete(request, pk):
    """Hides the item; it can be brought back with Undo until it is purged a day later."""
    extra = get_object_or_404(ExtraItem, pk=pk, household=request.household, removed_at=None)
    services.remove_extra_item(extra)
    return back_to_list(request, extra.week, removed=extra.pk)


@login_required
@require_POST
def extra_restore(request, pk):
    extra = get_object_or_404(ExtraItem, pk=pk, household=request.household)
    extra.removed_at = None
    extra.save(update_fields=["removed_at"])
    messages.success(request, f"{extra} is back on the list.")
    return back_to_list(request, extra.week)


@login_required
@require_POST
def weekly_skip(request, day, pk):
    """Takes an every-week item off this week's list only."""
    week = week_start(parse_date(day))
    item = get_object_or_404(WeeklyItem, pk=pk, household=request.household)
    services.skip_weekly_item(item, week)
    return back_to_list(request, week, skipped=item.pk)


@login_required
@require_POST
def weekly_unskip(request, day, pk):
    week = week_start(parse_date(day))
    item = get_object_or_404(WeeklyItem, pk=pk, household=request.household)
    services.unskip_weekly_item(item, week)
    messages.success(request, f"{item} is back on this week's list.")
    return back_to_list(request, week)
