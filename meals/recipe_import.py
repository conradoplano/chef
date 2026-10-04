"""
Reads a recipe with AI, from photos (a magazine page, a cookbook, a handwritten card)
or from a web page.

Runs in a background thread like menu planning. For a web page, the app downloads it
and passes on the recipe data most recipe sites embed (schema.org), or else the page's
text; if the site blocks downloads, the model opens it with web search. The model answers
through the strict `save_recipe` function. Nothing is stored as a recipe yet: the family
checks the result in a prefilled form first.
"""
import base64
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from datetime import timedelta
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import planner
from .models import Category, Dish, Household, RecipeImport

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
TIME_BUDGET = 8 * 60

SYSTEM_PROMPT = """You read recipes for a family's meal planning app, either from photos (a page from a food magazine or cookbook, or a handwritten card; there may be several photos of the same recipe) or from a web page.

- Copy the recipe faithfully: don't invent, improve or leave out ingredients or steps. If there is no recipe, set is_recipe to false.
- For a web page you're given its recipe data or text. If you only get the address, open it with web search and read the recipe there; if you can't reach it, set is_recipe to false.
- Write in English. If the recipe is in another language, translate it and put the original title in notes.
- Ingredients: one entry each, with quantity and unit as printed (metric where the recipe gives a choice). Use null for the quantity of things like "salt to taste". If any part of an ingredient is hard to read or you're unsure, set check to true so the family looks at it.
- Instructions: the method as numbered steps, one step per line, without the numbers.
- Source: for photos, the magazine or book, with issue, date and page if visible; for a web page, leave it empty.
- Servings: how many the recipe says it serves; minutes: total time if given, otherwise your estimate.
- For photos, use web search to find this same recipe published online (often on the magazine's own website). Only give online_url if you're confident it's the same recipe; otherwise leave it empty. For a web page, leave online_url empty.

Call save_recipe once with everything you read."""

SAVE_RECIPE_TOOL = {
    "type": "function",
    "name": "save_recipe",
    "description": "Save the recipe that was read, for the family to check.",
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


# --- web pages -----------------------------------------------------------------------

PAGE_TIMEOUT = 15
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_PAGE_TEXT = 40_000  # characters of page text passed to the model
RECIPE_KEYS = ["name", "description", "recipeYield", "totalTime", "prepTime", "cookTime", "recipeCategory",
               "recipeCuisine", "recipeIngredient", "recipeInstructions", "author", "publisher"]


def is_public(host):
    """Only pages on the public internet are fetched, never devices at home (router, NAS...)."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


class PublicRedirects(HTTPRedirectHandler):
    """Follows redirects only to other public addresses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public(urlsplit(newurl).hostname or ""):
            raise URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url):
    """Returns the page's HTML, or "" if it can't be downloaded (blocked, missing, not public)."""
    if not is_public(urlsplit(url).hostname or ""):
        return ""
    try:
        opener = build_opener(PublicRedirects)
        request = Request(url, headers={"User-Agent": planner.BROWSER_AGENT, "Accept-Language": "en,de;q=0.8"})
        with opener.open(request, timeout=PAGE_TIMEOUT) as response:
            if "html" not in response.headers.get("Content-Type", "html"):
                return ""
            raw = response.read(MAX_PAGE_BYTES)
            return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except (URLError, OSError, ValueError) as exc:
        logger.info("Couldn't download %s: %s", url, exc)
        return ""


def recipe_data(html):
    """The schema.org Recipe that most recipe sites embed for search engines, trimmed, or None."""
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                kinds = item.get("@type", [])
                if "Recipe" in (kinds if isinstance(kinds, list) else [kinds]):
                    return {k: item[k] for k in RECIPE_KEYS if k in item}
                stack.extend(item.get("@graph", []))
    return None


class PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}

    def __init__(self):
        super().__init__()
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping and data.strip():
            self.parts.append(" ".join(data.split()))


def page_text(html):
    parser = PageText()
    parser.feed(html)
    return "\n".join(parser.parts)[:MAX_PAGE_TEXT]


def page_content(url):
    """What the model gets for a recipe link."""
    html = fetch_page(url)
    data = recipe_data(html) if html else None
    if data:
        text = f"Recipe from {url} (the page's schema.org recipe data):\n{json.dumps(data, ensure_ascii=False)}"
    elif html:
        text = f"Recipe from {url}. Text of the page:\n{page_text(html)}"
    else:
        text = f"The recipe is at {url}, but the page couldn't be downloaded. Open it with web search and read the recipe there."
    return [{"type": "input_text", "text": text}]


def photo_content(photos):
    content = [{"type": "input_text", "text": f"Here {'is the photo' if len(photos) == 1 else f'are {len(photos)} photos'} of the recipe."}]
    return content + [photo_input(p) for p in photos]


# --- reading -------------------------------------------------------------------------


def ask_model(content):
    """Returns (parsed arguments, usage totals)."""
    client = planner.get_client()
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


def tidy(data, page_url=""):
    """Cleans the AI's answer for the review form: known values only, a working online link."""
    if page_url:
        url = planner.clean_url(page_url)
        data = {**data, "source": ""}  # the link itself says where it's from
    else:
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
            if job.url:
                content = page_content(job.url)
            else:
                photos = list(job.photos.all())
                if not photos:
                    raise planner.PlanningError("There are no photos to read.")
                content = photo_content(photos)
            data, usage = ask_model(content)
            job.input_tokens, job.output_tokens, job.web_searches = usage["input"], usage["output"], usage["searches"]
            job.cost = planner.cost(job.model, usage)
            if not data.get("is_recipe", True):
                raise planner.PlanningError(
                    "No recipe was found on that page." if job.url else "These photos don't seem to show a recipe."
                )
            result = tidy(data, job.url)
            # Decimals aren't JSON; the form turns these strings back into numbers.
            for ingredient in result["ingredients"]:
                ingredient["quantity"] = str(ingredient["quantity"]) if ingredient["quantity"] is not None else None
            job.result, job.status = result, RecipeImport.Status.DONE
        except planner.PlanningError as exc:
            job.status, job.error = RecipeImport.Status.FAILED, str(exc)
        except Exception as exc:
            logger.exception("Recipe import %s failed", import_id)
            job.status = RecipeImport.Status.FAILED
            job.error = f"Something went wrong while reading the recipe ({exc.__class__.__name__}). Please try again."
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
