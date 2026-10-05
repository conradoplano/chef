"""The recipe binder: favourites, recipes we want to try, every dish we have cooked, and adding recipes
from a link, photos or by hand."""
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .. import budget, photos, recipe_import
from ..services import plan_dish, search_recipes, with_history
from ..forms import UNITS, AddToMenuForm, IngredientForm, RecipeForm
from ..models import Dish, Ingredient, PlannedMeal, RecipeImport, RecipePhoto
from .common import parse_date, parse_plan, safe_next
from .menu import ingredient_names

FILTERS = [
    ("favourites", "★ Favourites", Q(status=Dish.Status.FAVOURITE)),
    ("try", "Want to try", Q(status=Dish.Status.TRY)),
    ("all", "All dishes", Q()),
]


@login_required
def recipes(request):
    show = request.GET.get("show", "favourites")
    if show not in {key for key, _, _ in FILTERS}:
        show = "favourites"
    query = " ".join(request.GET.get("q", "").split())
    household_dishes = Dish.objects.filter(household=request.household)
    dishes = household_dishes.filter(dict((k, f) for k, _, f in FILTERS)[show])
    if query:
        dishes = dishes.filter(Q(name__icontains=query) | Q(notes__icontains=query) | Q(ingredients__name__icontains=query)).distinct()
    order = "-saved_at" if show == "try" else "name"
    counts = {key: household_dishes.filter(f).count() for key, _, f in FILTERS}
    recipe_import.expire_stale()
    return render(request, "meals/recipes.html", {
        "imports": unsaved_imports(request.household),
        "dishes": with_history(dishes.order_by(order, "name")),
        "filters": [(key, label, counts[key]) for key, label, _ in FILTERS],
        "show": show,
        "query": query,
    })


@login_required
def recipe(request, pk):
    get_object_or_404(Dish, pk=pk, household=request.household)
    [dish] = with_history(Dish.objects.filter(pk=pk))
    form = AddToMenuForm(request.POST or None, initial={"date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
        meal = plan_dish(dish, day, slot, request.user)
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
    dish = get_object_or_404(Dish, pk=pk, household=request.household) if pk else None
    if request.method == "POST" and dish and "delete" in request.POST:
        if dish.planned.exists():
            dish.set_status(Dish.Status.NONE)
            messages.success(request, f"Removed {dish} from the binder; it stays in past menus.")
        else:
            dish.delete()
            messages.success(request, f"Deleted {dish}.")
        return redirect("meals:recipes")
    initial = {} if dish else {"status": Dish.Status.TRY, "name": request.GET.get("name", "")}
    form = RecipeForm(request.POST or None, instance=dish, initial=initial, household=request.household)
    if request.method == "POST" and form.is_valid():
        is_new = dish is None
        status_before = dish.status if dish else ""
        dish = form.save(commit=False)
        if dish.status and not status_before:
            dish.saved_at = timezone.now()
        dish.save()
        if is_new:
            plan = parse_plan(request.GET.get("plan"))
            after = reverse("meals:recipe", args=[dish.pk])
            if plan:
                meal = plan_dish(dish, *plan, request.user)
                after = safe_next(request) or reverse("meals:menu_of", args=[plan[0].isoformat()])
                messages.success(request, f"Saved {dish} and planned it for {meal.date:%a} {meal.get_slot_display().lower()}. "
                                          "Add its ingredients so it's on the shopping list.")
            else:
                messages.success(request, f"Saved {dish}. Add its ingredients so it's ready for the shopping list.")
            url = reverse("meals:ingredients", args=[dish.pk])
            return redirect(f"{url}?{urlencode({'next': after})}")
        messages.success(request, f"Saved {dish}.")
        return redirect("meals:recipe", pk=dish.pk)
    return render(request, "meals/recipe_edit.html", {"form": form, "dish": dish})


@login_required
@require_POST
def recipe_status(request, pk):
    """Sets the binder status; with toggle=favourite, stars or unstars (from the meal cards)."""
    dish = get_object_or_404(Dish, pk=pk, household=request.household)
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
        photo = RecipePhoto(household=request.household, dish=dish, recipe_import=job, uploaded_by=request.user)
        photo.image.save(image.name, image, save=True)
        saved.append(photo)
    return saved, []


URL_IN_TEXT = re.compile(r"https?://\S+")


def shared_url(request):
    """A recipe address passed in, e.g. by sharing a page to the app (Android): ?url= or inside ?text=."""
    for value in (request.GET.get("url", ""), request.GET.get("text", "")):
        match = URL_IN_TEXT.search(value)
        if match:
            return match.group(0).rstrip(").,;")
    return ""


@login_required
def recipe_add(request):
    """Three ways to add a recipe: from a link, from photos, or typed in."""
    plan = parse_plan(request.GET.get("plan"))
    return render(request, "meals/recipe_add.html", {
        "ai_blocked": budget.blocked(request.household),
        "url": shared_url(request),
        "plan": plan and f"{plan[0].isoformat()}:{plan[1]}",
        "plan_day": plan and plan[0],
        "plan_slot": plan and PlannedMeal.Slot(plan[1]).label.lower(),
        "next": safe_next(request),
    })


def start_import(request, **fields):
    """A RecipeImport that remembers the day it was started from, if any."""
    plan = parse_plan(request.POST.get("plan"))
    if plan:
        fields.update(plan_date=plan[0], plan_slot=plan[1], plan_next=safe_next(request)[:300])
    return RecipeImport.objects.create(household=request.household, created_by=request.user, **fields)


def back_to_add(request):
    """Back to the add page, keeping the day it was started from."""
    params = {k: request.POST[k] for k in ("plan", "next") if request.POST.get(k)}
    return redirect(reverse("meals:recipe_add") + (f"?{urlencode(params)}" if params else ""))


def same_page(a, b):
    def key(url):
        parts = urlsplit(url.strip().lower())
        return (parts.hostname or "").removeprefix("www.").removeprefix("tollbit."), parts.path.rstrip("/")
    return key(a) == key(b)


@login_required
@require_POST
def recipe_link_new(request):
    """A recipe page on the web, to be read by AI."""
    field = forms.URLField(max_length=500, assume_scheme="https")
    try:
        url = field.clean(request.POST.get("url", "").strip())
    except ValidationError:
        messages.error(request, "That doesn't look like a web address.")
        return back_to_add(request)
    known = next(
        (d for d in Dish.objects.filter(household=request.household).exclude(recipe_url="") if same_page(d.recipe_url, url)),
        None,
    )
    plan = parse_plan(request.POST.get("plan"))
    if known and plan:
        meal = plan_dish(known, *plan, request.user)
        messages.success(request, f"That recipe is already in your binder: planned {known} for {meal.date:%a} {meal.get_slot_display().lower()}.")
        return redirect(safe_next(request) or reverse("meals:menu_of", args=[plan[0].isoformat()]))
    if known:
        messages.info(request, f"That recipe is already in your binder: {known}.")
        return redirect("meals:recipe", pk=known.pk)
    waiting = next((j for j in unsaved_imports(request.household).exclude(url="") if same_page(j.url, url)), None)
    if waiting:
        return redirect("meals:recipe_import", pk=waiting.pk)
    blocked = budget.blocked(request.household)
    if blocked:
        messages.error(request, blocked)
        return back_to_add(request)
    job = start_import(request, url=url)
    recipe_import.start(job)
    return redirect("meals:recipe_import", pk=job.pk)


@login_required
def recipe_photo_new(request):
    """Photos of a recipe, e.g. a magazine page, to be read by AI. The form is on the add page."""
    if request.method != "POST":
        return redirect("meals:recipe_add")
    blocked = budget.blocked(request.household)
    if blocked:
        messages.error(request, blocked)
        return back_to_add(request)
    job = start_import(request)
    saved, errors = save_photos(request, job=job)
    if errors:
        job.delete()
        for error in errors:
            messages.error(request, error)
        return back_to_add(request)
    recipe_import.start(job)
    return redirect("meals:recipe_import", pk=job.pk)


class ImportedIngredientForm(IngredientForm):
    """A row prefilled with what the AI read. Django leaves out new rows that are unchanged from how they
    were prefilled, taking them for unused blank rows; a row the family accepts as it is, without a
    quantity ("a handful of mint"), would be lost. Here every row with a name is saved, and clearing the
    name leaves an ingredient out."""

    def has_changed(self):
        return bool(self.data.get(self.add_prefix("name"), "").strip())


def import_formset_class(count):
    return forms.inlineformset_factory(Dish, Ingredient, form=ImportedIngredientForm, extra=count, can_delete=True)


@login_required
def recipe_import_view(request, pk):
    recipe_import.expire_stale()
    job = get_object_or_404(RecipeImport, pk=pk, household=request.household)
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
    form = RecipeForm(request.POST or None, initial={"status": Dish.Status.TRY, "servings": 4, **initial},
                      household=request.household)
    Formset = import_formset_class(len(ingredients) + 1)
    formset = Formset(request.POST or None, instance=Dish(household=request.household), initial=ingredients)
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
            meal = plan_dish(dish, job.plan_date, job.plan_slot, request.user) if job.plan_date else None
        if meal:
            messages.success(request, f"Saved {dish} and planned it for {meal.date:%a} {meal.get_slot_display().lower()}.")
            return redirect(job.plan_next or reverse("meals:menu_of", args=[meal.date.isoformat()]))
        messages.success(request, f"Saved {dish}.")
        return redirect("meals:recipe", pk=dish.pk)
    context.update(
        form=form,
        formset=formset,
        checks=sum(1 for i in ingredients if i.get("check")),
        units=UNITS,
        names=ingredient_names(request.household),
    )
    return render(request, "meals/recipe_import.html", context)


@login_required
@require_POST
def recipe_import_retry(request, pk):
    job = get_object_or_404(RecipeImport, pk=pk, household=request.household, status=RecipeImport.Status.FAILED)
    job.status, job.error = RecipeImport.Status.PENDING, ""
    job.save(update_fields=["status", "error"])
    RecipeImport.objects.filter(pk=job.pk).update(created_at=timezone.now())  # a fresh time budget
    recipe_import.start(job)
    return redirect("meals:recipe_import", pk=job.pk)


@login_required
def recipe_import_status(request, pk):
    recipe_import.expire_stale()
    job = get_object_or_404(RecipeImport, pk=pk, household=request.household)
    # The review page is the same address, so "done" simply reloads it.
    return JsonResponse({
        "status": job.status, "finished": job.finished, "error": job.error,
        "url": reverse("meals:recipe_import", args=[job.pk]),
    })


@login_required
def recipe_photo_file(request, pk):
    """Photos are private: only shown to the household's members."""
    photo = get_object_or_404(RecipePhoto, pk=pk, household=request.household)
    try:
        response = FileResponse(photo.image.open("rb"), content_type="image/jpeg")
    except FileNotFoundError:
        raise Http404("Photo not found")
    response["Cache-Control"] = "private, max-age=86400"
    return response


@login_required
@require_POST
def recipe_photo_add(request, pk):
    dish = get_object_or_404(Dish, pk=pk, household=request.household)
    saved, errors = save_photos(request, dish=dish)
    for error in errors:
        messages.error(request, error)
    if saved:
        messages.success(request, f"Added {len(saved)} photo{'s' if len(saved) != 1 else ''}.")
    return redirect(reverse("meals:recipe", args=[dish.pk]) + "#photos")


@login_required
@require_POST
def recipe_photo_delete(request, pk):
    photo = get_object_or_404(RecipePhoto, pk=pk, household=request.household, dish__isnull=False)
    dish_pk = photo.dish_id
    photo.delete()
    messages.success(request, "Photo removed.")
    return redirect(reverse("meals:recipe", args=[dish_pk]) + "#photos")


def unsaved_imports(household):
    """Imports still waiting: being read, ready to check, or failed."""
    return RecipeImport.objects.filter(household=household).exclude(status=RecipeImport.Status.SAVED).prefetch_related("photos")


@login_required
@require_POST
def recipe_import_discard(request, pk):
    job = get_object_or_404(unsaved_imports(request.household), pk=pk)
    for photo in job.photos.filter(dish__isnull=True):
        photo.delete()  # also removes the file
    job.delete()
    messages.success(request, "Discarded.")
    return redirect("meals:recipes")


# --- choosing a recipe for a day ---------------------------------------------------------


@login_required
def meal_pick(request):
    """The day's "+ Add": choose a recipe we have (or leftovers), or add a new one."""
    day = parse_date(request.GET.get("date") or request.POST.get("date"))
    household = request.household
    back = safe_next(request) or reverse("meals:menu_of", args=[day.isoformat()])
    if request.method == "POST":
        slot = request.POST.get("slot") if request.POST.get("slot") in PlannedMeal.Slot.values else PlannedMeal.Slot.DINNER
        if "new" in request.POST:
            return redirect(reverse("meals:recipe_add") + "?" + urlencode({"plan": f"{day.isoformat()}:{slot}", "next": back}))
        if request.POST.get("leftover"):
            cooked = get_object_or_404(PlannedMeal, pk=request.POST["leftover"], household=household)
            meal = plan_dish(cooked.dish, day, slot, request.user, leftovers=True)
        elif request.POST.get("dish"):
            meal = plan_dish(get_object_or_404(Dish, pk=request.POST["dish"], household=household), day, slot, request.user)
        else:  # Enter in the search box without JavaScript: search
            return redirect(reverse("meals:meal_pick") + "?" + urlencode({"date": day.isoformat(), "slot": slot, "q": request.POST.get("q", ""), "next": back}))
        when = f"{meal.date:%a} {meal.get_slot_display().lower()}"
        if not meal.leftovers and not meal.dish.ingredients.exists():
            messages.success(request, f"Planned {meal.dish} for {when}. Add its ingredients so it's on the shopping list.")
            return redirect(reverse("meals:ingredients", args=[meal.dish.pk]) + "?" + urlencode({"next": back}))
        messages.success(request, f"Planned {'leftover ' if meal.leftovers else ''}{meal.dish} for {when}.")
        return redirect(back)

    planned = set(PlannedMeal.objects.filter(household=household, date=day).values_list("slot", flat=True))
    slot = request.GET.get("slot")
    if slot not in PlannedMeal.Slot.values:
        slot = PlannedMeal.Slot.LUNCH if PlannedMeal.Slot.DINNER in planned and PlannedMeal.Slot.LUNCH not in planned else PlannedMeal.Slot.DINNER
    query = " ".join(request.GET.get("q", "").split()).lower()

    dishes = search_recipes(household, query)
    groups = [
        ("★ Favourites", sorted([d for d in dishes if d.status == Dish.Status.FAVOURITE], key=lambda d: d.name.lower())),
        ("Want to try", sorted([d for d in dishes if d.status == Dish.Status.TRY], key=lambda d: d.name.lower())),
        ("Other dishes", sorted([d for d in dishes if not d.status],
                                key=lambda d: (d.last_cooked is None, -(d.last_cooked.toordinal() if d.last_cooked else 0), d.name.lower()))),
    ]
    week = day - timedelta(days=day.weekday())
    leftovers = (
        PlannedMeal.objects.filter(household=household, date__gte=week, date__lt=day, leftovers=False).select_related("dish").order_by("-date", "slot")
    )
    return render(request, "meals/meal_pick.html", {
        "day": day,
        "slot": slot,
        "slots": PlannedMeal.Slot.choices,
        "groups": [(title, items) for title, items in groups if items],
        "leftovers": leftovers,
        "query": query,
        "next": back,
        "count": len(dishes),
    })
