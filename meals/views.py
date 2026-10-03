from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from . import planner, schedule
from . import shopping as shopping_list
from .forms import (
    UNITS,
    DishServingsForm,
    ExtraItemForm,
    FamilyMemberForm,
    FeedbackForm,
    HouseholdForm,
    IngredientFormSet,
    PlannedMealForm,
    RuleForm,
)
from .models import (
    Dish,
    ExtraItem,
    FamilyMember,
    Feedback,
    Household,
    Ingredient,
    MenuRequest,
    PlannedMeal,
    Rule,
    ShoppingCheck,
)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Http404("Invalid date")


def week_start(day):
    return day - timedelta(days=day.weekday())


def week_context(day):
    """Monday-based week around `day`, with links to the neighbouring weeks."""
    today = timezone.localdate()
    start = week_start(parse_date(day) if day else today)
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
    meals = list(PlannedMeal.objects.filter(date__range=(start, end)).select_related("dish", "feedback"))
    for meal in meals:
        meal.review = getattr(meal, "feedback", None)
        # Feedback once it has been eaten; leftovers share the feedback of the day it was cooked.
        meal.can_review = meal.date <= today and not meal.leftovers
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


def safe_next(request):
    next_url = request.GET.get("next", "")
    return next_url if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}) else ""


def back_url(request, day):
    """The page the user came from (?next=), or else the week of `day`."""
    return safe_next(request) or reverse("meals:menu_of", args=[day.isoformat()])


def back_to(request, day):
    return redirect(back_url(request, day))


@login_required
def home(request):
    today = timezone.localdate()
    hour = timezone.localtime().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    sunday = week_start(today) + timedelta(days=6)
    return render(
        request,
        "meals/home.html",
        {
            "today": today,
            "greeting": greeting,
            "meals": planned_meals(today, today, today),
            "rest_of_week": days_with_meals(today + timedelta(days=1), sunday, today) if today < sunday else [],
            "next_week": sunday + timedelta(days=1),
        },
    )


@login_required
def menu(request, day=None):
    context = week_context(day)
    start, end = context["start"], context["end"]
    context["days"] = days_with_meals(start, end, context["today"])
    context["meal_count"] = sum(len(d["meals"]) for d in context["days"])
    planner.expire_stale()
    context["ai_enabled"] = planner.enabled()
    context["menu_request"] = latest = MenuRequest.objects.filter(week=start).first()
    # Show the summary unfolded right after the menu was created.
    context["about_open"] = bool(latest and latest.finished_at and timezone.now() - latest.finished_at < timedelta(hours=1))
    return render(request, "meals/menu.html", context)


@login_required
def meal_edit(request, pk=None):
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk) if pk else None
    if request.method == "POST" and meal and "delete" in request.POST:
        meal.delete()
        messages.success(request, f"Removed {meal.dish} from {meal.date:%a %d.%m.}")
        return back_to(request, meal.date)

    initial = {}
    if not meal:
        initial["date"] = parse_date(request.GET["date"]) if request.GET.get("date") else timezone.localdate()
        if request.GET.get("slot") in PlannedMeal.Slot.values:
            initial["slot"] = request.GET["slot"]
    form = PlannedMealForm(request.POST or None, instance=meal, initial=initial)
    if request.method == "POST" and form.is_valid():
        is_new = meal is None
        meal = form.save(commit=False)
        meal.updated_by = request.user
        meal.save()
        form.save_m2m()
        if is_new and not meal.leftovers and not meal.dish.ingredients.exists():
            # A dish without ingredients: they are needed for the shopping list.
            messages.success(request, f"Saved {meal.dish}. Now add its ingredients for the shopping list.")
            url = reverse("meals:ingredients", args=[meal.dish.pk])
            return redirect(f"{url}?{urlencode({'next': back_url(request, meal.date)})}")
        messages.success(request, f"Saved {meal.dish} on {meal.date:%a %d.%m.}")
        return back_to(request, meal.date)
    return render(
        request,
        "meals/meal_edit.html",
        {"form": form, "meal": meal, "dishes": Dish.objects.values_list("name", flat=True)},
    )


@login_required
def feedback(request, pk):
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk)
    review = Feedback.objects.filter(meal=meal).first()
    if request.method == "POST" and review and "delete" in request.POST:
        review.delete()
        messages.success(request, "Feedback removed.")
        return back_to(request, meal.date)
    form = FeedbackForm(request.POST or None, instance=review or Feedback(meal=meal))
    if request.method == "POST" and form.is_valid():
        review = form.save(commit=False)
        review.updated_by = request.user
        review.save()
        messages.success(request, f"Thanks! Saved feedback on {meal.dish}.")
        return back_to(request, meal.date)
    return render(request, "meals/feedback.html", {"form": form, "meal": meal, "review": review})


@login_required
@require_POST
def feedback_quick(request, pk):
    """One-tap feedback from the meal card: the same rating for kids and parents.
    Tapping the rating that is already set clears it again."""
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk)
    rating = request.POST.get("rating", "")
    if rating not in Feedback.Rating.values:
        raise Http404("Unknown rating")
    review = Feedback.objects.filter(meal=meal).first() or Feedback(meal=meal)
    if review.pk and review.shared_rating == rating:
        rating = ""
    review.kids = review.parents = rating
    review.updated_by = request.user
    if rating or review.reaction or review.notes:
        review.save()
    elif review.pk:
        review.delete()
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"rating": rating})
    return redirect(f"{back_url(request, meal.date)}#meal-{meal.pk}")


@login_required
def ingredients(request, pk):
    dish = get_object_or_404(Dish, pk=pk)
    formset = IngredientFormSet(request.POST or None, instance=dish)
    servings_form = DishServingsForm(request.POST or None, instance=dish, prefix="dish")
    if request.method == "POST" and formset.is_valid() and servings_form.is_valid():
        servings_form.save()
        formset.save()
        messages.success(request, f"Saved ingredients for {dish}.")
        return redirect(safe_next(request) or reverse("meals:shopping"))
    names = sorted(set(Ingredient.objects.values_list("name", flat=True)), key=str.lower)
    return render(
        request,
        "meals/ingredients.html",
        {"dish": dish, "formset": formset, "servings_form": servings_form, "units": UNITS, "names": names},
    )


@login_required
def shopping(request, day=None):
    context = week_context(day)
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


# --- family & rules -------------------------------------------------------


@login_required
def family(request):
    """Overview of family members, planning rules and household preferences."""
    rule_form = RuleForm(request.POST or None, initial={"active": True}, prefix="rule")
    if request.method == "POST" and rule_form.is_valid():
        rule = rule_form.save()
        messages.success(request, f"Added rule “{rule}”")
        return redirect("meals:family")
    household = Household.load()
    today = timezone.localdate()
    members = list(FamilyMember.objects.all())
    for member in members:
        member.age_now = member.age(today)
    return render(
        request,
        "meals/family.html",
        {
            "members": members,
            "usual_week": schedule.summary(schedule.usual_week(members), members),
            "rules": Rule.objects.all(),
            "rule_form": rule_form,
            "household": household,
            "household_rows": [
                (household._meta.get_field(name).verbose_name, value)
                for name, value in [
                    ("weekday_minutes", household.weekday_minutes and f"{household.weekday_minutes} min"),
                    ("weekend_minutes", household.weekend_minutes and f"{household.weekend_minutes} min"),
                    ("adventurousness", household.adventurousness and f"{household.adventurousness} / 10"),
                    ("cuisines", household.cuisines),
                    ("equipment", household.equipment),
                    ("shops", household.shops),
                    ("priority", household.get_priority_display()),
                    ("pantry", household.pantry),
                ]
                if value
            ],
        },
    )


@login_required
def member_edit(request, pk=None):
    member = get_object_or_404(FamilyMember, pk=pk) if pk else None
    if request.method == "POST" and member and "delete" in request.POST:
        member.delete()
        messages.success(request, f"Removed {member}")
        return redirect("meals:family")
    form = FamilyMemberForm(request.POST or None, instance=member)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"Saved {member}")
        return redirect("meals:family")
    return render(request, "meals/member_edit.html", {"form": form, "member": member})


@login_required
def rule_edit(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    if request.method == "POST" and "delete" in request.POST:
        rule.delete()
        messages.success(request, "Rule removed.")
        return redirect("meals:family")
    form = RuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Rule saved.")
        return redirect("meals:family")
    return render(request, "meals/rule_edit.html", {"form": form, "rule": rule})


@login_required
@require_POST
def rule_toggle(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    rule.active = not rule.active
    rule.save(update_fields=["active"])
    return redirect("meals:family")


@login_required
def household_edit(request):
    form = HouseholdForm(request.POST or None, instance=Household.load())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Household saved.")
        return redirect("meals:family")
    return render(request, "meals/household_edit.html", {"form": form})


# --- usual week & creating a menu with AI -------------------------------------


@login_required
def usual_week(request):
    """Which meals we usually need on each weekday, and who eats them."""
    members = list(FamilyMember.objects.all())
    keys = [(str(i), name) for i, name in enumerate(schedule.WEEKDAYS)]
    if request.method == "POST":
        grid, errors = schedule.parse_grid(request.POST, keys, members)
        if not errors:
            schedule.save_usual_week({int(k): v for k, v in grid.items()})
            messages.success(request, "Usual week saved.")
            return redirect("meals:family")
        for error in errors:
            messages.error(request, error)
    else:
        grid = {str(k): v for k, v in schedule.usual_week(members).items()}
    return render(
        request, "meals/usual_week.html", {"rows": schedule.rows(keys, grid, members), "members": members}
    )


@login_required
def menu_create(request, day):
    start = week_start(parse_date(day))
    planner.expire_stale()
    running = MenuRequest.objects.filter(
        week=start, status__in=[MenuRequest.Status.PENDING, MenuRequest.Status.RUNNING]
    ).first()
    if running:
        return redirect("meals:menu_request", pk=running.pk)

    members = list(FamilyMember.objects.all())
    keys = schedule.week_keys(start)
    existing = list(PlannedMeal.objects.filter(date__range=(start, start + timedelta(days=6))).select_related("dish"))
    details = request.POST.get("details", "")
    keep_existing = request.POST.get("replace") != "on" if request.method == "POST" else True
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
                created_by=request.user,
            )
            planner.start(menu_request)
            return redirect("meals:menu_request", pk=menu_request.pk)
        for error in errors:
            messages.error(request, error)
    else:
        usual = schedule.usual_week(members)
        grid = {key: usual[i] for i, (key, _) in enumerate(keys)}
    return render(
        request,
        "meals/menu_create.html",
        {
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
