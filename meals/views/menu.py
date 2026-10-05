"""Home, the week menu, editing meals, feedback and ingredients."""
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import planner
from ..forms import DishServingsForm, FeedbackForm, IngredientFormSet, PlannedMealForm, UNITS
from ..models import Dish, Feedback, Ingredient, MenuRequest, PlannedMeal, RecipeImport
from .common import (
    back_to,
    back_url,
    days_with_meals,
    leftovers_after,
    parse_date,
    planned_meals,
    safe_next,
    week_context,
    week_start,
)


@login_required
def home(request):
    today = timezone.localdate()
    hour = timezone.localtime().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    household = request.household
    sunday = week_start(today) + timedelta(days=6)
    if today < sunday:
        coming, coming_title, coming_week = days_with_meals(household, today + timedelta(days=1), sunday, today), "Rest of the week", week_start(today)
    else:
        # On Sundays, look ahead to the next working week.
        coming, coming_title, coming_week = days_with_meals(household, today + timedelta(days=1), today + timedelta(days=5), today), "Next week", today + timedelta(days=1)
    return render(
        request,
        "meals/home.html",
        {
            "today": today,
            "greeting": greeting,
            "meals": planned_meals(household, today, today, today),
            "coming": coming,
            "coming_title": coming_title,
            "coming_week": coming_week,
            "ready_imports": RecipeImport.objects.filter(household=household, status=RecipeImport.Status.DONE).count(),
        },
    )


@login_required
def menu(request, day=None):
    context = week_context(request, day)
    start, end = context["start"], context["end"]
    household = request.household
    context["days"] = days_with_meals(household, start, end, context["today"])
    context["meal_count"] = sum(len(d["meals"]) for d in context["days"])
    context["from_sources"] = recipes_from_sources(household, [m for d in context["days"] for m in d["meals"]])
    planner.expire_stale()
    context["menu_request"] = MenuRequest.objects.filter(household=household, week=start).first()
    # "About this menu" comes from the last time the whole menu was created or changed; replacing one dish keeps it.
    context["about"] = (
        MenuRequest.objects.filter(
            household=household, week=start, kind__in=[MenuRequest.Kind.CREATE, MenuRequest.Kind.CHANGE],
            status=MenuRequest.Status.DONE,
        )
        .exclude(summary="")
        .first()
    )
    return render(request, "meals/menu.html", context)


@login_required
def meal_edit(request, pk=None):
    household = request.household
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk, household=household) if pk else None
    leftovers = leftovers_after(meal) if meal else []
    if request.method == "POST" and meal and "delete" in request.POST:
        meal.delete()
        removed = f"Removed {meal.dish} from {meal.date:%a %d.%m.}"
        if leftovers and request.POST.get("with_leftovers") == "on":
            PlannedMeal.objects.filter(pk__in=[m.pk for m in leftovers]).delete()
            removed += f" and its leftovers ({', '.join(f'{m.date:%a} {m.get_slot_display().lower()}' for m in leftovers)})"
        messages.success(request, removed + ".")
        return back_to(request, meal.date)

    initial = {}
    if not meal:
        initial["date"] = parse_date(request.GET["date"]) if request.GET.get("date") else timezone.localdate()
        if request.GET.get("slot") in PlannedMeal.Slot.values:
            initial["slot"] = request.GET["slot"]
    form = PlannedMealForm(request.POST or None, instance=meal, initial=initial, household=household)
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
        {"form": form, "meal": meal, "leftovers": leftovers, "dishes": Dish.objects.filter(household=household).values_list("name", flat=True)},
    )


@login_required
def feedback(request, pk):
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk, household=request.household)
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
        if meal.dish.learn_from(review):
            messages.success(request, f"★ Everyone liked it, so {meal.dish} is now a favourite.")
        return back_to(request, meal.date)
    return render(request, "meals/feedback.html", {"form": form, "meal": meal, "review": review})


@login_required
@require_POST
def feedback_quick(request, pk):
    """One-tap feedback from the meal card: the same rating for kids and parents.
    Tapping the rating that is already set clears it again."""
    meal = get_object_or_404(PlannedMeal.objects.select_related("dish"), pk=pk, household=request.household)
    rating = request.POST.get("rating", "")
    if rating not in Feedback.Rating.values:
        raise Http404("Unknown rating")
    review = Feedback.objects.filter(meal=meal).first() or Feedback(meal=meal)
    if review.pk and review.shared_rating == rating:
        rating = ""
    review.kids = review.parents = rating
    review.updated_by = request.user
    promoted = False
    if rating or review.reaction or review.notes:
        review.save()
        promoted = meal.dish.learn_from(review)
    elif review.pk:
        review.delete()
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"rating": rating, "favourite": promoted})
    return redirect(f"{back_url(request, meal.date)}#meal-{meal.pk}")


@login_required
def ingredients(request, pk):
    dish = get_object_or_404(Dish, pk=pk, household=request.household)
    formset = IngredientFormSet(request.POST or None, instance=dish)
    servings_form = DishServingsForm(request.POST or None, instance=dish, prefix="dish")
    if request.method == "POST" and formset.is_valid() and servings_form.is_valid():
        servings_form.save()
        formset.save()
        messages.success(request, f"Saved ingredients for {dish}.")
        return redirect(safe_next(request) or reverse("meals:shopping"))
    names = ingredient_names(request.household)
    return render(
        request,
        "meals/ingredients.html",
        {"dish": dish, "formset": formset, "servings_form": servings_form, "units": UNITS, "names": names},
    )


@login_required
def menu_copy(request, day):
    """Copies the meals of an earlier week into this one, keeping weekday and meal."""
    start = week_start(parse_date(day))
    meals = PlannedMeal.objects.filter(household=request.household)
    if request.method == "POST":
        source = week_start(parse_date(request.POST.get("source")))
        taken = {(m.date, m.slot) for m in meals.filter(date__range=(start, start + timedelta(days=6)))}
        copied = skipped = 0
        offset = start - source
        for meal in meals.filter(date__range=(source, source + timedelta(days=6))).prefetch_related("eaters"):
            target = meal.date + offset
            if (target, meal.slot) in taken:
                skipped += 1
                continue
            copy = PlannedMeal.objects.create(
                household=request.household, date=target, slot=meal.slot, dish_id=meal.dish_id, leftovers=meal.leftovers, note=meal.note,
                servings=meal.servings, updated_by=request.user,
            )
            copy.eaters.set(meal.eaters.all())
            copied += 1
        text = f"Copied {copied} meal{'s' if copied != 1 else ''} from the week of {source.day} {source:%b}"
        if skipped:
            text += f" ({skipped} skipped – already planned)"
        messages.success(request, text + ".")
        return redirect("meals:menu_of", day=start.isoformat())

    weeks = {}
    for meal in meals.exclude(date__range=(start, start + timedelta(days=6))).select_related("dish").order_by("-date"):
        week = week_start(meal.date)
        if week not in weeks and len(weeks) >= 12:
            break
        weeks.setdefault(week, []).append(meal)
    choices = [
        {"start": week, "end": week + timedelta(days=6), "count": len(meals),
         "dishes": list(dict.fromkeys(m.dish.name for m in sorted(meals, key=lambda m: (m.date, m.slot != "lunch")) if not m.leftovers))}
        for week, meals in weeks.items()
    ]
    return render(request, "meals/menu_copy.html", {"start": start, "end": start + timedelta(days=6), "weeks": choices})


def recipes_from_sources(household, meals):
    """(from your recipe websites, recipes with a link) for cooked meals, or None without websites.
    Only websites can be checked; names like "Jamie Oliver" have no known address."""
    domains, _ = planner.recipe_sources(household.recipe_sites)
    if not domains:
        return None
    hosts = [
        (urlsplit(m.dish.recipe_url).hostname or "").lower().removeprefix("www.")
        for m in meals if not m.leftovers and m.dish.recipe_url
    ]
    if not hosts:
        return None
    ours = sum(any(h == d or h.endswith("." + d) for d in domains) for h in hosts)
    return {"ours": ours, "total": len(hosts)}


def ingredient_names(household):
    """Ingredient names the household has used, as suggestions."""
    return sorted(set(Ingredient.objects.filter(dish__household=household).values_list("name", flat=True)), key=str.lower)
