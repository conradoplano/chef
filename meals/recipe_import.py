"""
Reads a recipe from photos (a magazine page, a cookbook, a handwritten card) with AI.

Runs in a background thread like menu planning. The model gets the photos, may search
the web for the same recipe published online, and answers through the strict
`save_recipe` function. Nothing is stored as a recipe yet: the family checks the
result in a prefilled form first.
"""
import base64
import json
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import planner
from .models import Category, Dish, Household, RecipeImport

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
TIME_BUDGET = 8 * 60

SYSTEM_PROMPT = """You read recipes from photos for a family's meal planning app. The photos show one recipe, for example a page from a food magazine or cookbook, or a handwritten card; there may be several photos of the same recipe.

- Copy the recipe faithfully: don't invent, improve or leave out ingredients or steps. If the photos don't show a recipe, set is_recipe to false.
- Write in English. If the recipe is in another language, translate it and put the original title in notes.
- Ingredients: one entry each, with quantity and unit as printed (metric where the recipe gives a choice). Use null for the quantity of things like "salt to taste". If any part of an ingredient is hard to read or you're unsure, set check to true so the family looks at it.
- Instructions: the method as numbered steps, one step per line, without the numbers.
- Source: the magazine or book, with issue, date and page if visible.
- Servings: how many the recipe says it serves; minutes: total time if given, otherwise your estimate.
- Use web search to find this same recipe published online (often on the magazine's own website). Only give online_url if you're confident it's the same recipe; otherwise leave it empty.

Call save_recipe once with everything you read."""

SAVE_RECIPE_TOOL = {
    "type": "function",
    "name": "save_recipe",
    "description": "Save the recipe read from the photos, for the family to check.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["is_recipe", "name", "kind", "minutes", "servings", "source", "online_url", "notes",
                     "instructions", "ingredients"],
        "properties": {
            "is_recipe": {"type": "boolean"},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": planner.KINDS},
            "minutes": {"type": ["integer", "null"]},
            "servings": {"type": ["integer", "null"]},
            "source": {"type": "string"},
            "online_url": {"type": "string"},
            "notes": {"type": "string", "description": "Anything useful that isn't a step, e.g. tips or the original title."},
            "instructions": {"type": "array", "items": {"type": "string"}},
            "ingredients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "quantity", "unit", "category", "note", "check"],
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": "string"},
                        "category": {"type": "string", "enum": [c for c, _ in Category.choices]},
                        "note": {"type": "string"},
                        "check": {"type": "boolean", "description": "True if it was hard to read or you're unsure."},
                    },
                },
            },
        },
    },
}


def photo_input(photo):
    with photo.image.open("rb") as f:
        data = base64.b64encode(f.read()).decode()
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}", "detail": "high"}


def ask_model(photos):
    """Returns (parsed arguments, usage totals)."""
    client = planner.get_client()
    content = [{"type": "input_text", "text": f"Here {'is the photo' if len(photos) == 1 else f'are {len(photos)} photos'} of the recipe."}]
    content += [photo_input(p) for p in photos]
    usage = {"input": 0, "output": 0, "searches": 0}
    previous_id, next_input = None, [{"role": "user", "content": content}]
    deadline = time.monotonic() + TIME_BUDGET
    for _ in range(2):
        remaining = deadline - time.monotonic()
        if remaining < 30:
            break
        response = client.with_options(timeout=remaining).responses.create(
            model=settings.AI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=next_input,
            tools=[planner.web_search_tool(Household.load()), SAVE_RECIPE_TOOL],
            tool_choice="auto",
            reasoning={"effort": settings.AI_EFFORT},
            max_output_tokens=32000,
            **({"previous_response_id": previous_id} if previous_id else {}),
        )
        if response.usage:
            usage["input"] += response.usage.input_tokens or 0
            usage["output"] += response.usage.output_tokens or 0
        usage["searches"] += sum(1 for i in response.output if i.type == "web_search_call")
        call = next((i for i in response.output if i.type == "function_call" and i.name == "save_recipe"), None)
        if call is not None:
            try:
                return json.loads(call.arguments), usage
            except json.JSONDecodeError:
                raise planner.PlanningError("The AI's answer couldn't be read. Please try again.")
        if response.status == "incomplete":
            raise planner.PlanningError("The AI stopped before finishing. Please try again.")
        if any(part.type == "refusal" for item in response.output if item.type == "message" for part in item.content):
            raise planner.PlanningError("The AI declined to read these photos.")
        previous_id = response.id
        next_input = [{"role": "user", "content": "Please call save_recipe with the recipe now."}]
    raise planner.PlanningError("The AI didn't return a recipe. Please try again.")


def tidy(data):
    """Cleans the AI's answer for the review form: known values only, a working online link."""
    url = planner.clean_url(str(data.get("online_url", "")))
    if not url.startswith(("http://", "https://")) or not planner.link_works(url):
        url = ""
    return {
        "name": " ".join(str(data.get("name", "")).split())[:200],
        "kind": data.get("kind") if data.get("kind") in planner.KINDS else Dish.Kind.OTHER,
        "minutes": max(0, min(int(data["minutes"]), 1000)) if data.get("minutes") else None,
        "servings": max(1, min(int(data["servings"]), 100)) if data.get("servings") else 4,
        "source": str(data.get("source", "")).strip()[:200],
        "recipe_url": url[:500],
        "notes": str(data.get("notes", "")).strip(),
        "instructions": "\n".join(s.strip() for s in data.get("instructions", []) if str(s).strip()),
        "ingredients": [
            {
                "name": " ".join(str(i.get("name", "")).split())[:100],
                "quantity": planner._decimal(i.get("quantity")),
                "unit": str(i.get("unit", ""))[:20],
                "category": i.get("category") if i.get("category") in dict(Category.choices) else Category.OTHER,
                "note": str(i.get("note", ""))[:200],
                "check": bool(i.get("check")),
            }
            for i in data.get("ingredients", [])
            if str(i.get("name", "")).strip()
        ],
    }


def run(import_id):
    """Reads the photos of a RecipeImport. Safe to run in a thread."""
    try:
        job = RecipeImport.objects.get(pk=import_id)
        job.status = RecipeImport.Status.RUNNING
        job.model = settings.AI_MODEL
        job.save(update_fields=["status", "model"])
        try:
            photos = list(job.photos.all())
            if not photos:
                raise planner.PlanningError("There are no photos to read.")
            data, usage = ask_model(photos)
            job.input_tokens, job.output_tokens, job.web_searches = usage["input"], usage["output"], usage["searches"]
            job.cost = planner.cost(job.model, usage)
            if not data.get("is_recipe", True):
                raise planner.PlanningError("These photos don't seem to show a recipe.")
            result = tidy(data)
            # Decimals aren't JSON; the form turns these strings back into numbers.
            for ingredient in result["ingredients"]:
                ingredient["quantity"] = str(ingredient["quantity"]) if ingredient["quantity"] is not None else None
            job.result, job.status = result, RecipeImport.Status.DONE
        except planner.PlanningError as exc:
            job.status, job.error = RecipeImport.Status.FAILED, str(exc)
        except Exception as exc:
            logger.exception("Recipe import %s failed", import_id)
            job.status = RecipeImport.Status.FAILED
            job.error = f"Something went wrong while reading the photos ({exc.__class__.__name__}). Please try again."
        job.finished_at = timezone.now()
        job.save()
    finally:
        close_old_connections()


def start(job):
    threading.Thread(target=run, args=(job.pk,), daemon=True, name=f"recipe-import-{job.pk}").start()


def expire_stale():
    RecipeImport.objects.filter(
        status__in=[RecipeImport.Status.PENDING, RecipeImport.Status.RUNNING],
        created_at__lt=timezone.now() - STALE_AFTER,
    ).update(status=RecipeImport.Status.FAILED, error="This took too long and was stopped. Please try again.",
             finished_at=timezone.now())
