"""The recipe binder: favourites, recipes we want to try, every dish we have cooked, and recipes from photos."""
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import photos, planner, recipe_import, schedule
from ..forms import UNITS, AddToMenuForm, IngredientForm, RecipeForm
from ..models import Dish, FamilyMember, Feedback, Ingredient, PlannedMeal, RecipeImport, RecipePhoto
from .common import safe_next

FILTERS = [
    ("favourites", "★ Favourites", Q(status=Dish.Status.FAVOURITE)),
    ("try", "Want to try", Q(status=Dish.Status.TRY)),
    ("all", "All dishes", Q()),
]


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


# --- recipes from photos ----------------------------------------------------------------


def save_photos(request, dish=None, job=None):
    """Stores the uploaded photos (scaled down). Returns (photos, errors)."""
    files = request.FILES.getlist("photos")
    errors = []
    if not files:
        errors.append("Choose at least one photo.")
    if len(files) > settings.RECIPE_PHOTOS_PER_IMPORT:
        errors.append(f"At most {settings.RECIPE_PHOTOS_PER_IMPORT} photos at a time.")
    processed = []
    for upload in files[: settings.RECIPE_PHOTOS_PER_IMPORT]:
        try:
            processed.append(photos.process(upload))
        except ValidationError as exc:
            errors.extend(exc.messages)
    if errors:
        return [], errors
    saved = []
    for image in processed:
        photo = RecipePhoto(dish=dish, recipe_import=job, uploaded_by=request.user)
        photo.image.save(image.name, image, save=True)
        saved.append(photo)
    return saved, []


@login_required
def recipe_photo_new(request):
    """Photos of a recipe, e.g. a magazine page, to be read by AI."""
    if request.method == "POST":
        if not planner.enabled():
            messages.error(request, "Reading recipes with AI isn't set up yet (OPENAI_API_KEY is missing).")
            return redirect("meals:recipe_photo_new")
        job = RecipeImport.objects.create(created_by=request.user)
        saved, errors = save_photos(request, job=job)
        if errors:
            job.delete()
            for error in errors:
                messages.error(request, error)
        else:
            recipe_import.start(job)
            return redirect("meals:recipe_import", pk=job.pk)
    return render(request, "meals/recipe_photo.html", {"ai_enabled": planner.enabled()})


def import_formset_class(count):
    return forms.inlineformset_factory(Dish, Ingredient, form=IngredientForm, extra=count, can_delete=True)


@login_required
def recipe_import_view(request, pk):
    recipe_import.expire_stale()
    job = get_object_or_404(RecipeImport, pk=pk)
    if job.status == RecipeImport.Status.SAVED and job.dish:
        return redirect("meals:recipe", pk=job.dish.pk)
    manual = request.GET.get("manual") == "1"
    context = {"job": job, "photos": job.photos.all(), "manual": manual}
    if not job.finished or (job.status == RecipeImport.Status.FAILED and not manual):
        return render(request, "meals/recipe_import.html", context)

    # Check what the AI read, then save it as a recipe.
    result = job.result if job.status == RecipeImport.Status.DONE else {}
    ingredients = result.get("ingredients", [])
    initial = {k: result.get(k) for k in ("name", "kind", "minutes", "servings", "source", "recipe_url", "notes", "instructions") if result.get(k) is not None}
    form = RecipeForm(request.POST or None, initial={"status": Dish.Status.TRY, "servings": 4, **initial})
    Formset = import_formset_class(len(ingredients) + 1)
    formset = Formset(request.POST or None, instance=Dish(), initial=ingredients)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            dish = form.save(commit=False)
            if dish.status:
                dish.saved_at = timezone.now()
            dish.save()
            formset.instance = dish
            formset.save()
            job.photos.update(dish=dish)
            job.dish, job.status = dish, RecipeImport.Status.SAVED
            job.save(update_fields=["dish", "status"])
        messages.success(request, f"Saved {dish}.")
        return redirect("meals:recipe", pk=dish.pk)
    context.update(
        form=form,
        formset=formset,
        checks=sum(1 for i in ingredients if i.get("check")),
        units=UNITS,
        names=sorted(set(Ingredient.objects.values_list("name", flat=True)), key=str.lower),
    )
    return render(request, "meals/recipe_import.html", context)


@login_required
@require_POST
def recipe_import_retry(request, pk):
    job = get_object_or_404(RecipeImport, pk=pk, status=RecipeImport.Status.FAILED)
    job.status, job.error = RecipeImport.Status.PENDING, ""
    job.save(update_fields=["status", "error"])
    RecipeImport.objects.filter(pk=job.pk).update(created_at=timezone.now())  # a fresh time budget
    recipe_import.start(job)
    return redirect("meals:recipe_import", pk=job.pk)


@login_required
def recipe_import_status(request, pk):
    recipe_import.expire_stale()
    job = get_object_or_404(RecipeImport, pk=pk)
    # The review page is the same address, so "done" simply reloads it.
    return JsonResponse({
        "status": job.status, "finished": job.finished, "error": job.error,
        "url": reverse("meals:recipe_import", args=[job.pk]),
    })


@login_required
def recipe_photo_file(request, pk):
    """Photos are private: only shown to logged-in family members."""
    photo = get_object_or_404(RecipePhoto, pk=pk)
    try:
        response = FileResponse(photo.image.open("rb"), content_type="image/jpeg")
    except FileNotFoundError:
        raise Http404("Photo not found")
    response["Cache-Control"] = "private, max-age=86400"
    return response


@login_required
@require_POST
def recipe_photo_add(request, pk):
    dish = get_object_or_404(Dish, pk=pk)
    saved, errors = save_photos(request, dish=dish)
    for error in errors:
        messages.error(request, error)
    if saved:
        messages.success(request, f"Added {len(saved)} photo{'s' if len(saved) != 1 else ''}.")
    return redirect(reverse("meals:recipe", args=[dish.pk]) + "#photos")


@login_required
@require_POST
def recipe_photo_delete(request, pk):
    photo = get_object_or_404(RecipePhoto, pk=pk, dish__isnull=False)
    dish_pk = photo.dish_id
    photo.delete()
    messages.success(request, "Photo removed.")
    return redirect(reverse("meals:recipe", args=[dish_pk]) + "#photos")
