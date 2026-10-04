"""Recipes from photos: storing photos, reading them with (fake) AI, checking and saving."""
import io
import shutil
import tempfile
from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User

from . import photos, planner, recipe_import
from .models import Dish, PlannedMeal, RecipeImport, RecipePhoto
from .test_planner import FakeOpenAI, message, reply


def image_file(name="page.jpg", size=(3000, 1500), fmt="JPEG", orientation=None):
    image = Image.new("RGB", size, "white")
    out = io.BytesIO()
    kwargs = {}
    if orientation:
        exif = image.getexif()
        exif[0x0112] = orientation  # rotated phone photo
        exif[0x0110] = "Test phone"  # camera model: metadata like this (or location) must not be kept
        kwargs["exif"] = exif
    image.save(out, fmt, **kwargs)
    return SimpleUploadedFile(name, out.getvalue(), content_type=f"image/{fmt.lower()}")


def recipe_call(**overrides):
    data = {
        "is_recipe": True, "name": "Lemon chicken traybake", "kind": "meat", "minutes": 50, "servings": 4,
        "source": "BBC Good Food magazine, Oct 2026, p. 42", "online_url": "https://tollbit.bbcgoodfood.com/recipes/lemon-chicken",
        "notes": "", "instructions": ["Heat the oven to 200C.", "Roast everything for 40 min."],
        "ingredients": [
            {"name": "Chicken thighs", "quantity": 800, "unit": "g", "category": "meat", "note": "", "check": False},
            {"name": "Lemons", "quantity": 2, "unit": "pcs", "category": "vegetables", "note": "", "check": True},
        ],
    }
    data.update(overrides)
    import json
    from types import SimpleNamespace

    return SimpleNamespace(type="function_call", name="save_recipe", arguments=json.dumps(data), call_id="c1")


class PhotoProcessingTests(TestCase):
    def test_scaled_rotated_and_stripped(self):
        result = Image.open(io.BytesIO(photos.process(image_file(orientation=6)).read()))
        self.assertEqual(result.size, (1000, 2000))  # turned upright, longest side 2000
        self.assertEqual(dict(result.getexif()), {})  # no location or other metadata

    def test_png_becomes_jpeg(self):
        content = photos.process(image_file("card.png", (400, 300), "PNG"))
        self.assertEqual(content.name, "card.jpg")
        self.assertEqual(Image.open(io.BytesIO(content.read())).format, "JPEG")

    def test_not_an_image(self):
        with self.assertRaisesMessage(ValidationError, "couldn't be read"):
            photos.process(SimpleUploadedFile("recipe.pdf", b"%PDF-1.4 not an image"))


class RecipeFromPhotoTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        for patcher in [
            override_settings(MEDIA_ROOT=self.media, OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini"),
            mock.patch.object(recipe_import, "start", side_effect=lambda job: recipe_import.run(job.pk)),
            mock.patch.object(planner, "link_works", return_value=True),
        ]:
            patcher.enable() if hasattr(patcher, "enable") else patcher.start()
            self.addCleanup(patcher.disable if hasattr(patcher, "disable") else patcher.stop)
        self.user = User.objects.create_user(email="parent@example.com")
        self.client.force_login(self.user)

    def _fake(self, *replies):
        fake = FakeOpenAI(*replies)
        patcher = mock.patch.object(planner, "get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _upload(self, *files):
        return self.client.post(reverse("meals:recipe_photo_new"), {"photos": list(files) or [image_file()]})

    def test_read_check_and_save(self):
        fake = self._fake(reply(SimpleSearch(), recipe_call()))
        response = self._upload(image_file("p1.jpg"), image_file("p2.jpg"))
        job = RecipeImport.objects.get()
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]))
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual((job.web_searches, job.photos.count()), (1, 2))
        # Both photos went to the AI as images.
        content = fake.requests[0]["input"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["input_text", "input_image", "input_image"])
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))

        review = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(review, 'value="Lemon chicken traybake"')
        self.assertContains(review, "https://www.bbcgoodfood.com/recipes/lemon-chicken")  # tollbit cleaned up
        self.assertContains(review, "⚠ 1 ingredient to double-check")
        self.assertContains(review, "Hard to read")
        self.assertContains(review, "Heat the oven to 200C.\nRoast everything for 40 min.")

        data = {
            "name": "Lemon chicken traybake", "recipe_url": "https://www.bbcgoodfood.com/recipes/lemon-chicken",
            "source": "BBC Good Food magazine, Oct 2026, p. 42", "kind": "meat", "minutes": "50", "servings": "4",
            "instructions": "Heat the oven to 200C.\nRoast everything for 40 min.", "notes": "", "status": "try",
            "ingredients-TOTAL_FORMS": "3", "ingredients-INITIAL_FORMS": "0",
            "ingredients-MIN_NUM_FORMS": "0", "ingredients-MAX_NUM_FORMS": "1000",
            "ingredients-0-name": "Chicken thighs", "ingredients-0-quantity": "800", "ingredients-0-unit": "g",
            "ingredients-0-category": "meat", "ingredients-0-note": "",
            "ingredients-1-name": "Lemons", "ingredients-1-quantity": "3", "ingredients-1-unit": "pcs",
            "ingredients-1-category": "vegetables", "ingredients-1-note": "",
            "ingredients-2-name": "", "ingredients-2-quantity": "", "ingredients-2-unit": "",
            "ingredients-2-category": "other", "ingredients-2-note": "",
        }
        response = self.client.post(reverse("meals:recipe_import", args=[job.pk]), data)
        dish = Dish.objects.get(name="Lemon chicken traybake")
        self.assertRedirects(response, reverse("meals:recipe", args=[dish.pk]))
        self.assertEqual((dish.status, dish.source, dish.steps[0]), ("try", "BBC Good Food magazine, Oct 2026, p. 42", "Heat the oven to 200C."))
        self.assertEqual(sorted(dish.ingredients.values_list("name", "quantity")), [("Chicken thighs", 800), ("Lemons", 3)])
        self.assertEqual(dish.photos.count(), 2)
        job.refresh_from_db()
        self.assertEqual((job.status, job.dish), ("saved", dish))
        # Coming back to the import goes to the recipe.
        self.assertRedirects(self.client.get(reverse("meals:recipe_import", args=[job.pk])), reverse("meals:recipe", args=[dish.pk]))

        page = self.client.get(reverse("meals:recipe", args=[dish.pk]))
        self.assertContains(page, "<li>Roast everything for 40 min.</li>")
        self.assertContains(page, "📷 BBC Good Food magazine, Oct 2026, p. 42")
        self.assertContains(page, reverse("meals:photo", args=[dish.photos.first().pk]))

    def test_not_a_recipe_and_retry(self):
        self._fake(reply(recipe_call(is_recipe=False)), reply(recipe_call()))
        self._upload()
        job = RecipeImport.objects.get()
        self.assertEqual(job.status, "failed")
        page = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(page, "don&#x27;t seem to show a recipe")
        self.assertContains(page, "Fill it in yourself")
        self.client.post(reverse("meals:recipe_import_retry", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, "done")

    def test_fill_in_by_hand_after_a_failure(self):
        self._fake(reply(message("I can't read this", refusal=True)))
        self._upload()
        job = RecipeImport.objects.get()
        page = self.client.get(reverse("meals:recipe_import", args=[job.pk]) + "?manual=1")
        self.assertContains(page, "Check the recipe")
        self.assertContains(page, 'name="instructions"')

    def test_upload_validation(self):
        self._fake()
        response = self.client.post(reverse("meals:recipe_photo_new"), {"photos": [image_file(f"p{i}.jpg", (50, 50)) for i in range(7)]}, follow=True)
        self.assertContains(response, "At most 6 photos")
        response = self.client.post(reverse("meals:recipe_photo_new"), {"photos": [SimpleUploadedFile("x.txt", b"hello")]}, follow=True)
        self.assertContains(response, "couldn&#x27;t be read")
        self.assertFalse(RecipeImport.objects.exists())
        self.assertFalse(RecipePhoto.objects.exists())

    @override_settings(OPENAI_API_KEY="")
    def test_needs_the_api_key(self):
        response = self.client.get(reverse("meals:recipe_photo_new"))
        self.assertRedirects(response, reverse("meals:recipe_add"))
        self.assertContains(self.client.get(reverse("meals:recipe_add")), "isn't set up yet")

    def test_photos_are_private(self):
        dish = Dish.objects.create(name="Soup")
        self.client.post(reverse("meals:recipe_photo_add", args=[dish.pk]), {"photos": [image_file(size=(400, 300))]})
        photo = dish.photos.get()
        response = self.client.get(reverse("meals:photo", args=[photo.pk]))
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("meals:photo", args=[photo.pk])).status_code, 302)

    def test_removing_a_photo_deletes_the_file(self):
        dish = Dish.objects.create(name="Soup")
        self.client.post(reverse("meals:recipe_photo_add", args=[dish.pk]), {"photos": [image_file(size=(400, 300))]})
        photo = dish.photos.get()
        path = photo.image.path
        self.client.post(reverse("meals:photo_delete", args=[photo.pk]))
        self.assertFalse(RecipePhoto.objects.exists())
        import os
        self.assertFalse(os.path.exists(path))

    def test_cards_open_recipes_the_app_holds(self):
        dish = Dish.objects.create(name="Gran's stew", instructions="Brown the meat.\nSimmer 2 hours.", source="Gran's notebook")
        PlannedMeal.objects.create(date=date(2026, 10, 5), dish=dish)
        page = self.client.get(reverse("meals:menu_of", args=["2026-10-05"]))
        self.assertContains(page, f'<a class="meal-link" href="{reverse("meals:recipe", args=[dish.pk])}">')
        self.assertContains(page, "📷 Gran&#x27;s notebook")
        dish.ingredients.create(name="Beef", quantity=500, unit="g", category="meat")
        shop = self.client.get(reverse("meals:shopping_of", args=["2026-10-05"]))
        self.assertContains(shop, f'<a class="badge use" href="{reverse("meals:recipe", args=[dish.pk])}"')

    def test_ai_cost_is_kept(self):
        self._fake(reply(recipe_call()))
        self._upload()
        job = RecipeImport.objects.get()
        # 1000 input and 500 output tokens at $0.75 / $4.50 per million.
        self.assertEqual(str(job.cost), "0.0030")


class SimpleSearch:
    type = "web_search_call"
    status = "completed"


class UnfinishedImportTests(TestCase):
    """Leaving the waiting page mustn't lose a recipe: unfinished imports stay reachable."""

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        patcher = override_settings(MEDIA_ROOT=self.media)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client.force_login(User.objects.create_user(email="parent@example.com"))

    def _job(self, status, **fields):
        job = RecipeImport.objects.create(status=status, **fields)
        photo = RecipePhoto(recipe_import=job)
        photo.image.save("page.jpg", photos.process(image_file(size=(400, 300))), save=True)
        return job

    def test_recipes_page_lists_unsaved_imports(self):
        ready = self._job("done", result={"name": "Butter bean bake"})
        reading = self._job("running")
        failed = self._job("failed", error="Blurry")
        saved = self._job("saved", dish=Dish.objects.create(name="Done already"))
        page = self.client.get(reverse("meals:recipes"))
        self.assertContains(page, "Being added")
        self.assertContains(page, "Butter bean bake")
        self.assertContains(page, "Ready to check ›")
        self.assertContains(page, "Reading…")
        self.assertContains(page, "Didn't work")
        for job in (ready, reading, failed):
            self.assertContains(page, reverse("meals:recipe_import", args=[job.pk]))
        self.assertNotContains(page, reverse("meals:recipe_import", args=[saved.pk]))
        # Something still being read can't be discarded.
        self.assertNotContains(page, reverse("meals:recipe_import_discard", args=[reading.pk]))

    def test_home_reminds_when_ready(self):
        self.assertNotContains(self.client.get(reverse("meals:home")), "ready to check")
        self._job("done", result={"name": "Bake"})
        self.assertContains(self.client.get(reverse("meals:home")), "1 new recipe ready to check")

    def test_discard_removes_photos_and_files(self):
        job = self._job("done", result={"name": "Bake"})
        path = job.photos.get().image.path
        response = self.client.post(reverse("meals:recipe_import_discard", args=[job.pk]))
        self.assertRedirects(response, reverse("meals:recipes"))
        self.assertFalse(RecipeImport.objects.exists())
        self.assertFalse(RecipePhoto.objects.exists())
        import os
        self.assertFalse(os.path.exists(path))

    def test_saved_imports_cannot_be_discarded(self):
        job = self._job("saved", dish=Dish.objects.create(name="Kept"))
        self.assertEqual(self.client.post(reverse("meals:recipe_import_discard", args=[job.pk])).status_code, 404)
        self.assertEqual(RecipePhoto.objects.count(), 1)


PAGE_WITH_RECIPE_DATA = """<html><head><title>Pad thai</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Good Food"},
  {"@type": ["Recipe"], "name": "Easy pad thai", "recipeYield": "4", "totalTime": "PT30M",
   "recipeIngredient": ["200g rice noodles", "2 eggs"], "recipeInstructions": [{"@type": "HowToStep", "text": "Soak the noodles."}],
   "image": "https://example.com/huge.jpg", "review": [{"text": "lots of reviews"}]}]}</script>
</head><body><p>Lots of chatter about my holiday.</p></body></html>"""


class PageReadingTests(TestCase):
    def test_uses_the_embedded_recipe_data(self):
        with mock.patch.object(recipe_import, "fetch_page", return_value=PAGE_WITH_RECIPE_DATA):
            text = recipe_import.page_content("https://example.com/pad-thai")[0]["text"]
        self.assertIn("schema.org recipe data", text)
        self.assertIn('"recipeIngredient": ["200g rice noodles", "2 eggs"]', text)
        self.assertNotIn("holiday", text)  # no page chatter
        self.assertNotIn("reviews", text)  # only the recipe's own fields

    def test_falls_back_to_the_page_text(self):
        html = "<html><body><script>var x=1;</script><h1>Gran's soup</h1><p>Boil   the water.</p></body></html>"
        with mock.patch.object(recipe_import, "fetch_page", return_value=html):
            text = recipe_import.page_content("https://example.com/soup")[0]["text"]
        self.assertIn("Text of the page:\nGran's soup\nBoil the water.", text)
        self.assertNotIn("var x", text)

    def test_blocked_pages_are_opened_with_web_search(self):
        with mock.patch.object(recipe_import, "fetch_page", return_value=""):
            text = recipe_import.page_content("https://blocked.example/r")[0]["text"]
        self.assertIn("couldn't be downloaded. Open it with web search", text)

    def test_only_public_addresses_are_fetched(self):
        for host in ["localhost", "127.0.0.1", "192.168.1.1", "10.0.0.5", "nas.local-does-not-exist.invalid"]:
            self.assertFalse(recipe_import.is_public(host), host)
        with mock.patch.object(recipe_import, "build_opener") as opener:
            self.assertEqual(recipe_import.fetch_page("http://192.168.1.1/admin"), "")
        opener.assert_not_called()


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini")
class RecipeFromLinkTests(TestCase):
    def setUp(self):
        for patcher in [
            mock.patch.object(recipe_import, "start", side_effect=lambda job: recipe_import.run(job.pk)),
            mock.patch.object(recipe_import, "fetch_page", return_value=PAGE_WITH_RECIPE_DATA),
            mock.patch.object(planner, "link_works", return_value=True),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(User.objects.create_user(email="parent@example.com"))

    def _fake(self, *replies):
        fake = FakeOpenAI(*replies)
        patcher = mock.patch.object(planner, "get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_add_page_offers_three_ways(self):
        page = self.client.get(reverse("meals:recipe_add"))
        for text in ["🔗 From a link", "📷 From photos", "✍️ Type it in"]:
            self.assertContains(page, text)
        self.assertContains(self.client.get(reverse("meals:recipes")), f'href="{reverse("meals:recipe_add")}">+ Add recipe</a>')

    def test_shared_links_are_filled_in(self):
        page = self.client.get(reverse("meals:recipe_add") + "?title=Pad+thai&text=Look+at+this+https%3A%2F%2Fexample.com%2Fpad-thai%2F+yum")
        self.assertContains(page, 'value="https://example.com/pad-thai/"')
        manifest = self.client.get(reverse("core:manifest")).json()
        self.assertEqual(manifest["share_target"]["action"], reverse("meals:recipe_add"))

    def test_read_check_and_save(self):
        fake = self._fake(reply(recipe_call(name="Easy pad thai", source="Some magazine", online_url="https://other.example/x")))
        response = self.client.post(reverse("meals:recipe_link_new"), {"url": "example.com/pad-thai"})
        job = RecipeImport.objects.get()
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]))
        self.assertEqual((job.url, job.status), ("https://example.com/pad-thai", "done"))
        self.assertIn("schema.org recipe data", fake.requests[0]["input"][0]["content"][0]["text"])
        # The link is the recipe's link; no source line, no other link.
        self.assertEqual((job.result["recipe_url"], job.result["source"]), ("https://example.com/pad-thai", ""))
        review = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(review, "copied the recipe from the page")
        self.assertContains(review, 'value="Easy pad thai"')

    def test_known_link_goes_to_the_recipe(self):
        dish = Dish.objects.create(name="Pad thai", recipe_url="https://www.example.com/pad-thai/")
        response = self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/pad-thai"})
        self.assertRedirects(response, reverse("meals:recipe", args=[dish.pk]))
        self.assertFalse(RecipeImport.objects.exists())

    def test_link_being_read_is_not_read_twice(self):
        job = RecipeImport.objects.create(url="https://example.com/pad-thai", status="running")
        response = self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/pad-thai/"})
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]))
        self.assertEqual(RecipeImport.objects.count(), 1)

    def test_not_a_web_address(self):
        response = self.client.post(reverse("meals:recipe_link_new"), {"url": "just some words"}, follow=True)
        self.assertContains(response, "doesn&#x27;t look like a web address")

    def test_page_without_a_recipe(self):
        self._fake(reply(recipe_call(is_recipe=False)))
        self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/about"})
        self.assertEqual(RecipeImport.objects.get().error, "No recipe was found on that page.")

    def test_listed_while_being_added(self):
        RecipeImport.objects.create(url="https://www.example.com/pad-thai", status="running")
        page = self.client.get(reverse("meals:recipes"))
        self.assertContains(page, "example.com/pad-thai")
        self.assertContains(page, "🔗")
