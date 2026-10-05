"""
What can be done with a household's menus, shopping lists and recipes, shared by the web pages and
the assistant tools (connect/tools.py), so both check, scope and record changes the same way.
Every function works within one household; callers look up objects in that household first.
"""
from datetime import timedelta

from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone

from . import schedule
from .models import Dish, ExtraItem, FamilyMember, Feedback, PlannedMeal, RecipePhoto, ShoppingCheck, WeeklySkip


def week_start(day):
    return day - timedelta(days=day.weekday())


# --- menu ---------------------------------------------------------------------------------


def plan_dish(dish, day, slot, user, leftovers=False):
    """Puts a dish on its household's menu, for the people who usually eat that meal."""
    household = dish.household
    meal = PlannedMeal.objects.create(
        household=household, date=day, slot=slot, dish=dish, leftovers=leftovers, updated_by=user
    )
    members = list(FamilyMember.objects.filter(household=household))
    meal.eaters.set(schedule.usual_week(household, members)[day.weekday()][slot]["eaters"])
    return meal


def leftovers_after(meal):
    """Later meals this week that are leftovers of a cooked meal; they go wherever the meal goes."""
    if meal.leftovers:
        return []
    return list(
        PlannedMeal.objects.filter(
            household=meal.household_id, dish=meal.dish, leftovers=True, date__gt=meal.date,
            date__lte=week_start(meal.date) + timedelta(days=6),
        ).prefetch_related("eaters")
    )


def remove_meal(meal, with_leftovers=False):
    """Takes a meal off the menu; returns the leftover meals removed with it."""
    leftovers = leftovers_after(meal) if with_leftovers else []
    meal.delete()
    PlannedMeal.objects.filter(pk__in=[m.pk for m in leftovers]).delete()
    return leftovers


def copy_week(household, user, source, target):
    """Copies the meals of the week starting on `source` into the week starting on `target`, keeping weekday
    and meal; slots that already have a meal are skipped. Returns (copied, skipped)."""
    meals = PlannedMeal.objects.filter(household=household)
    taken = {(m.date, m.slot) for m in meals.filter(date__range=(target, target + timedelta(days=6)))}
    copied = skipped = 0
    for meal in meals.filter(date__range=(source, source + timedelta(days=6))).prefetch_related("eaters"):
        day = meal.date + (target - source)
        if (day, meal.slot) in taken:
            skipped += 1
            continue
        copy = PlannedMeal.objects.create(
            household=household, date=day, slot=meal.slot, dish_id=meal.dish_id, leftovers=meal.leftovers,
            note=meal.note, servings=meal.servings, updated_by=user,
        )
        copy.eaters.set(meal.eaters.all())
        copied += 1
    return copied, skipped


def save_feedback(meal, user, kids="", parents="", reaction=None, notes=None):
    """Sets how a meal went (None keeps reaction / notes as they are); feedback with nothing in it is removed.
    Returns (feedback or None, True if the dish just became a favourite)."""
    review = Feedback.objects.filter(meal=meal).first() or Feedback(meal=meal)
    review.kids, review.parents = kids, parents
    if reaction is not None:
        review.reaction = reaction
    if notes is not None:
        review.notes = notes
    review.updated_by = user
    if review.kids or review.parents or review.reaction or review.notes:
        review.save()
        return review, meal.dish.learn_from(review)
    if review.pk:
        review.delete()
    return None, False


# --- shopping list -------------------------------------------------------------------------


def add_extra_item(household, user, week, name, quantity="", category="other", note=""):
    return ExtraItem.objects.create(
        household=household, week=week, name=name, quantity=quantity, category=category, note=note, created_by=user
    )


def remove_extra_item(extra):
    """Hides the item; it can be brought back (undo) until it is purged a day later."""
    now = timezone.now()
    extra.removed_at = now
    extra.save(update_fields=["removed_at"])
    for old in ExtraItem.objects.filter(removed_at__lt=now - timedelta(days=1)):
        ShoppingCheck.objects.filter(household=old.household_id, week=old.week, key=f"extra-{old.pk}").delete()
        old.delete()


def skip_weekly_item(item, week):
    """Takes an every-week item off one week's list; it's back the week after."""
    WeeklySkip.objects.get_or_create(item=item, week=week)


def unskip_weekly_item(item, week):
    WeeklySkip.objects.filter(item=item, week=week).delete()


def set_bought(household, user, week, key, bought):
    """Marks one item as bought or not. Sets the wanted state rather than flipping, so two people
    tapping at the same time don't undo each other."""
    if bought:
        ShoppingCheck.objects.update_or_create(household=household, week=week, key=key, defaults={"checked_by": user})
    else:
        ShoppingCheck.objects.filter(household=household, week=week, key=key).delete()


# --- recipes -------------------------------------------------------------------------------


def with_history(dishes):
    """Adds times cooked, last cooked and the latest feedback to each dish."""
    dishes = list(
        dishes.prefetch_related(Prefetch("photos", queryset=RecipePhoto.objects.only("pk", "dish"))).annotate(
            cooked=Count("planned", filter=Q(planned__leftovers=False)),
            last_cooked=Max("planned__date", filter=Q(planned__leftovers=False, planned__date__lte=timezone.localdate())),
        )
    )
    latest = {}
    for review in Feedback.objects.filter(meal__dish__in=dishes).select_related("meal").order_by("meal__date"):
        latest[review.meal.dish_id] = review
    for dish in dishes:
        dish.latest_feedback = latest.get(dish.pk)
    return dishes


def search_text(dish):
    parts = [dish.name, dish.notes, dish.source, dish.get_kind_display(), *(i.name for i in dish.ingredients.all())]
    return " ".join(parts).lower()


def search_recipes(household, query="", dishes=None):
    """The household's dishes (with history) matching every word of the query in name, ingredients,
    notes, source or type."""
    dishes = with_history((dishes if dishes is not None else Dish.objects.filter(household=household)).prefetch_related("ingredients"))
    for dish in dishes:
        dish.search = search_text(dish)
    words = query.lower().split()
    return [d for d in dishes if all(word in d.search for word in words)]
