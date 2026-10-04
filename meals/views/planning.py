"""Creating, changing and partly replacing a week's menu with AI."""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .. import planner, schedule
from ..models import FamilyMember, MenuRequest, PlannedMeal
from .common import leftovers_after, parse_date, running_request, week_start


@login_required
def menu_create(request, day):
    start = week_start(parse_date(day))
    running = running_request(start)
    if running:
        return redirect("meals:menu_request", pk=running.pk)

    members = list(FamilyMember.objects.all())
    keys = schedule.week_keys(start)
    existing = list(
        PlannedMeal.objects.filter(date__range=(start, start + timedelta(days=6)))
        .select_related("dish")
        .prefetch_related("eaters")
    )
    # With meals already planned this is "Change menu": by default everything is planned anew.
    changing = bool(existing)
    details = request.POST.get("details", "")
    keep_existing = request.POST.get("replace") != "on" if request.method == "POST" else not changing
    if request.method == "POST":
        grid, errors = schedule.parse_grid(request.POST, keys, members)
        slots = [
            {"date": key, "slot": slot, "eaters": entry["eaters"]}
            for key, _ in keys
            for slot, entry in grid[key].items()
            if entry["on"]
        ]
        if not slots:
            errors.append("Choose at least one meal to plan.")
        if not planner.enabled():
            errors.append("Menu planning with AI isn't set up yet (OPENAI_API_KEY is missing).")
        if not errors:
            menu_request = MenuRequest.objects.create(
                week=start, slots=slots, keep_existing=keep_existing, details=details.strip(),
                kind=MenuRequest.Kind.CHANGE if changing else MenuRequest.Kind.CREATE,
                created_by=request.user,
            )
            planner.start(menu_request)
            return redirect("meals:menu_request", pk=menu_request.pk)
        for error in errors:
            messages.error(request, error)
    else:
        usual = schedule.usual_week(members)
        grid = {key: {slot: dict(entry) for slot, entry in usual[i].items()} for i, (key, _) in enumerate(keys)}
        for meal in existing:
            # Every planned meal is ticked, for the people it was planned for.
            entry = grid[meal.date.isoformat()][meal.slot]
            entry["on"] = True
            eaters = [m.pk for m in meal.eaters.all()]
            if eaters:
                entry["eaters"] = eaters
        # Days already past start unticked, so eaten meals (and their feedback) aren't replaced by accident.
        today = timezone.localdate()
        for key, _ in keys:
            if date.fromisoformat(key) < today:
                for entry in grid[key].values():
                    entry["on"] = False
    return render(
        request,
        "meals/menu_create.html",
        {
            "changing": changing,
            "start": start,
            "end": start + timedelta(days=6),
            "rows": schedule.rows(keys, grid, members),
            "members": members,
            "existing": existing,
            "details": details,
            "keep_existing": keep_existing,
            "ai_enabled": planner.enabled(),
        },
    )


@login_required
def menu_request(request, pk):
    planner.expire_stale()
    menu_request = get_object_or_404(MenuRequest, pk=pk)
    if menu_request.status == MenuRequest.Status.DONE:
        return redirect("meals:menu_of", day=menu_request.week.isoformat())
    return render(request, "meals/menu_request.html", {"menu_request": menu_request})


@login_required
def menu_request_status(request, pk):
    planner.expire_stale()
    menu_request = get_object_or_404(MenuRequest, pk=pk)
    return JsonResponse({
        "status": menu_request.status,
        "finished": menu_request.finished,
        "error": menu_request.error,
        "url": reverse("meals:menu_of", args=[menu_request.week.isoformat()]),
    })


@login_required
def meal_replace(request, pk):
    """Asks the AI for a different dish for one meal, keeping the rest of the week."""
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish").prefetch_related("eaters"), pk=pk)
    start = week_start(meal.date)
    running = running_request(start)
    if running:
        return redirect("meals:menu_request", pk=running.pk)
    if request.method == "POST":
        if not planner.enabled():
            messages.error(request, "Menu planning with AI isn't set up yet (OPENAI_API_KEY is missing).")
            return redirect("meals:menu_of", day=meal.date.isoformat())
        # Leftovers of this dish later in the week go with it.
        meals = [meal, *leftovers_after(meal)]
        usual = schedule.usual_week(list(FamilyMember.objects.all()))
        slots = [
            {
                "date": m.date.isoformat(),
                "slot": m.slot,
                "eaters": [e.pk for e in m.eaters.all()] or usual[m.date.weekday()][m.slot]["eaters"],
            }
            for m in meals
        ]
        menu_request = MenuRequest.objects.create(
            week=start, kind=MenuRequest.Kind.REPLACE, slots=slots, keep_existing=False,
            replacing=meal.dish.name, details=request.POST.get("reason", "").strip()[:1000],
            created_by=request.user,
        )
        planner.start(menu_request)
        return redirect("meals:menu_request", pk=menu_request.pk)
    return render(request, "meals/meal_replace.html", {"meal": meal, "ai_enabled": planner.enabled()})
