"""The recipe binder: favourites, recipes we want to try, and every dish we have cooked."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import schedule
from ..forms import AddToMenuForm, RecipeForm
from ..models import Dish, FamilyMember, Feedback, PlannedMeal
from .common import safe_next

FILTERS = [
    ("favourites", "★ Favourites", Q(status=Dish.Status.FAVOURITE)),
    ("try", "Want to try", Q(status=Dish.Status.TRY)),
    ("all", "All dishes", Q()),
]


def with_history(dishes):
    """Adds times cooked, last cooked and the latest feedback to each dish."""
    dishes = list(
        dishes.annotate(
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


@login_required
def recipes(request):
    show = request.GET.get("show", "favourites")
    if show not in {key for key, _, _ in FILTERS}:
        show = "favourites"
    query = " ".join(request.GET.get("q", "").split())
    dishes = Dish.objects.filter(dict((k, f) for k, _, f in FILTERS)[show])
    if query:
        dishes = dishes.filter(Q(name__icontains=query) | Q(notes__icontains=query) | Q(ingredients__name__icontains=query)).distinct()
    order = "-saved_at" if show == "try" else "name"
    counts = {key: Dish.objects.filter(f).count() for key, _, f in FILTERS}
    return render(request, "meals/recipes.html", {
        "dishes": with_history(dishes.order_by(order, "name")),
        "filters": [(key, label, counts[key]) for key, label, _ in FILTERS],
        "show": show,
        "query": query,
    })


@login_required
def recipe(request, pk):
    dish = get_object_or_404(Dish, pk=pk)
    [dish] = with_history(Dish.objects.filter(pk=pk))
    form = AddToMenuForm(request.POST or None, initial={"date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
        meal = PlannedMeal.objects.create(date=day, slot=slot, dish=dish, updated_by=request.user)
        # Planned for the people who usually eat that meal.
        members = list(FamilyMember.objects.all())
        meal.eaters.set(schedule.usual_week(members)[day.weekday()][slot]["eaters"])
        messages.success(request, f"Added {dish} to {day:%a %d.%m.} ({meal.get_slot_display().lower()}).")
        return redirect("meals:menu_of", day=day.isoformat())
    history = (
        PlannedMeal.objects.filter(dish=dish, leftovers=False).select_related("feedback").order_by("-date")[:20]
    )
    for meal in history:
        meal.review = getattr(meal, "feedback", None)
    return render(request, "meals/recipe.html", {
        "dish": dish,
        "form": form,
        "history": history,
        "ingredients": dish.ingredients.all(),
        "statuses": list(reversed(Dish.Status.choices)),  # Favourite, Want to try, Not in the binder
    })


@login_required
def recipe_edit(request, pk=None):
    dish = get_object_or_404(Dish, pk=pk) if pk else None
    if request.method == "POST" and dish and "delete" in request.POST:
        if dish.planned.exists():
            dish.set_status(Dish.Status.NONE)
            messages.success(request, f"Removed {dish} from the binder; it stays in past menus.")
        else:
            dish.delete()
            messages.success(request, f"Deleted {dish}.")
        return redirect("meals:recipes")
    initial = {} if dish else {"status": Dish.Status.TRY, "name": request.GET.get("name", "")}
    form = RecipeForm(request.POST or None, instance=dish, initial=initial)
    if request.method == "POST" and form.is_valid():
        is_new = dish is None
        status_before = dish.status if dish else ""
        dish = form.save(commit=False)
        if dish.status and not status_before:
            dish.saved_at = timezone.now()
        dish.save()
        if is_new:
            messages.success(request, f"Saved {dish}. Add its ingredients so it's ready for the shopping list.")
            url = reverse("meals:ingredients", args=[dish.pk])
            return redirect(f"{url}?{urlencode({'next': reverse('meals:recipe', args=[dish.pk])})}")
        messages.success(request, f"Saved {dish}.")
        return redirect("meals:recipe", pk=dish.pk)
    return render(request, "meals/recipe_edit.html", {"form": form, "dish": dish})


@login_required
@require_POST
def recipe_status(request, pk):
    """Sets the binder status; with toggle=favourite, stars or unstars (from the meal cards)."""
    dish = get_object_or_404(Dish, pk=pk)
    if request.POST.get("toggle") == "favourite":
        status = Dish.Status.NONE if dish.status == Dish.Status.FAVOURITE else Dish.Status.FAVOURITE
    else:
        status = request.POST.get("status", "")
    if status in Dish.Status.values:
        dish.set_status(status)
        messages.success(request, {
            Dish.Status.FAVOURITE: f"★ {dish} is a favourite.",
            Dish.Status.TRY: f"{dish} is on the list to try.",
            Dish.Status.NONE: f"Removed {dish} from the binder.",
        }[status])
    anchor = request.POST.get("anchor", "")
    return redirect((safe_next(request) or reverse("meals:recipe", args=[dish.pk])) + (f"#{anchor}" if anchor.isidentifier() or anchor.startswith("meal-") else ""))
