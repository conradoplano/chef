"""Helpers shared by the views: weeks, meal cards, leftovers and safe redirects."""
from datetime import date, timedelta

from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .. import planner
from ..models import Feedback, MenuRequest, PlannedMeal


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Http404("Invalid date")


def week_start(day):
    return day - timedelta(days=day.weekday())


WEEK_SESSION_KEY = "selected_week"


def selected_week(request, day):
    """The week to show. A week opened explicitly (?day in the URL) is remembered for this browser
    for the rest of the day, so Menu and Shopping stay on it; otherwise the current week."""
    today = timezone.localdate()
    if day:
        start = week_start(parse_date(day))
        request.session[WEEK_SESSION_KEY] = {"start": start.isoformat(), "on": today.isoformat()}
        return start
    saved = request.session.get(WEEK_SESSION_KEY) or {}
    if saved.get("on") == today.isoformat():
        try:
            return date.fromisoformat(saved["start"])
        except (KeyError, ValueError):
            pass
    return week_start(today)


def week_context(request, day):
    """Monday-based week to show, with links to the neighbouring weeks."""
    today = timezone.localdate()
    start = selected_week(request, day)
    return {
        "start": start,
        "end": start + timedelta(days=6),
        "prev": start - timedelta(days=7),
        "next": start + timedelta(days=7),
        "this_week": week_start(today),
        "today": today,
    }


def planned_meals(start, end, today):
    """Meals between two dates, prepared for the meal card template."""
    meals = list(
        PlannedMeal.objects.filter(date__range=(start, end)).select_related("dish", "feedback").prefetch_related("dish__photos")
    )
    for meal in meals:
        meal.review = getattr(meal, "feedback", None)
        # Feedback once it has been eaten; leftovers share the feedback of the day it was cooked.
        meal.can_review = meal.date <= today and not meal.leftovers
        meal.can_replace = meal.date >= today
        shared = meal.review.shared_rating if meal.review else ""
        meal.quick_ratings = [
            {"value": value, "emoji": Feedback.EMOJI[value], "label": label.split(" ", 1)[1], "on": value == shared}
            for value, label in Feedback.Rating.choices
        ]
    return meals


def days_with_meals(start, end, today):
    """[{date, meals}] for every day from start to end, for the day template."""
    by_date = {}
    for meal in planned_meals(start, end, today):
        by_date.setdefault(meal.date, []).append(meal)
    return [{"date": start + timedelta(days=i), "meals": by_date.get(start + timedelta(days=i), [])}
            for i in range((end - start).days + 1)]


def leftovers_after(meal):
    """Later meals this week that are leftovers of a cooked meal; they go wherever the meal goes."""
    if meal.leftovers:
        return []
    return list(
        PlannedMeal.objects.filter(
            dish=meal.dish, leftovers=True, date__gt=meal.date, date__lte=week_start(meal.date) + timedelta(days=6)
        ).prefetch_related("eaters")
    )


def plan_dish(dish, day, slot, user, leftovers=False):
    """Puts a dish on the menu, for the people who usually eat that meal."""
    from .. import schedule
    from ..models import FamilyMember

    meal = PlannedMeal.objects.create(date=day, slot=slot, dish=dish, leftovers=leftovers, updated_by=user)
    meal.eaters.set(schedule.usual_week(list(FamilyMember.objects.all()))[day.weekday()][slot]["eaters"])
    return meal


def parse_plan(value):
    """"2026-10-06:dinner" -> (date, slot), or None. Carries "add it to this day" through adding a recipe."""
    day, _, slot = (value or "").partition(":")
    try:
        day = date.fromisoformat(day)
    except ValueError:
        return None
    return (day, slot) if slot in PlannedMeal.Slot.values else None


def safe_next(request):
    next_url = request.GET.get("next", "") or request.POST.get("next", "")
    return next_url if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}) else ""


def back_url(request, day):
    """The page the user came from (?next=), or else the week of `day`."""
    return safe_next(request) or reverse("meals:menu_of", args=[day.isoformat()])


def back_to(request, day):
    return redirect(back_url(request, day))


def running_request(week):
    """The menu request in progress for a week, if any (stale ones are expired first)."""
    planner.expire_stale()
    return MenuRequest.objects.filter(
        week=week, status__in=[MenuRequest.Status.PENDING, MenuRequest.Status.RUNNING]
    ).first()
