"""Weekly shopping items, and choosing a recipe for a day."""
from datetime import date, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import planner, recipe_import, shopping
from .models import Dish, FamilyMember, PlannedMeal, RecipeImport, ShoppingCheck, WeeklyItem
from .testing import approve_ai, home
from .test_planner import FakeOpenAI, reply
from .test_photos import recipe_call

WEEK = date(2026, 10, 5)


class WeeklyItemTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com", name="Pat"))

    def _add(self, **data):
        fields = {"weekly-name": "Fruit", "weekly-quantity": "3 kg", "weekly-category": "vegetables"}
        fields.update({f"weekly-{k}": v for k, v in data.items()})
        return self.client.post(reverse("meals:weekly_item_add"), fields, follow=True)

    def _items(self, week=WEEK):
        sections, at_home, _ = shopping.build(home(), week)
        return {i.name: i for s in sections for i in s.items}

    def test_managed_in_settings(self):
        response = self._add()
        self.assertContains(response, "Fruit is on the shopping list every week now.")
        self.assertContains(response, "🔁 Every week")
        self.assertContains(response, "<strong>Fruit</strong> <span class=\"qty\">3 kg</span>")
        item = WeeklyItem.objects.get()
        self.client.post(reverse("meals:weekly_item_delete", args=[item.pk]))
        self.assertFalse(WeeklyItem.objects.exists())

    def test_on_every_week_from_when_it_was_added(self):
        item = WeeklyItem.objects.create(household=home(), name="Bread", quantity="2 loaves", category="bakery")
        WeeklyItem.objects.filter(pk=item.pk).update(created_at=timezone.make_aware(timezone.datetime(2026, 10, 7, 12)))
        self.assertEqual(self._items(WEEK)["Bread"].quantity, "2 loaves")  # the week it was added
        self.assertIn("Bread", self._items(WEEK + timedelta(weeks=3)))
        self.assertNotIn("Bread", self._items(WEEK - timedelta(weeks=1)))  # earlier weeks don't change

    def test_shown_like_a_meal_chip_without_skipping(self):
        WeeklyItem.objects.create(household=home(), name="Kids' snacks", category="breakfast")
        page = self.client.get(reverse("meals:shopping_of", args=[WEEK.isoformat()]))
        self.assertContains(page, '<span class="badge use">Every week</span>')
        self.assertNotContains(page, "Skip this week")

    def test_remove_this_week_only_and_undo(self):
        item = WeeklyItem.objects.create(household=home(), name="Fruit", quantity="3 kg", category="vegetables")
        url = reverse("meals:shopping_of", args=[WEEK.isoformat()])
        page = self.client.get(url).content.decode()
        line = page[page.index(f'data-key="weekly-{item.pk}"'):]
        line = line[:line.index("</li>")]
        self.assertIn("✕ Remove this week</button>", line[line.index('class="item-tools"'):])  # hidden with the buttons

        response = self.client.post(reverse("meals:weekly_skip", args=["2026-10-07", item.pk]), follow=True)
        self.assertRedirects(response, f"{url}?skipped={item.pk}")
        self.assertContains(response, "Removed <strong>Fruit</strong> from this week")
        self.assertNotIn("Fruit", self._items(WEEK))
        self.assertIn("Fruit", self._items(WEEK + timedelta(weeks=1)))  # back next week
        self.assertTrue(WeeklyItem.objects.filter(pk=item.pk).exists())

        response = self.client.post(reverse("meals:weekly_unskip", args=[WEEK.isoformat(), item.pk]), follow=True)
        self.assertContains(response, "Fruit is back on this week")
        self.assertIn("Fruit", self._items(WEEK))

    def test_remove_this_week_only_for_our_household(self):
        theirs = WeeklyItem.objects.create(household=User.objects.create_user(email="sam@example.com").household, name="Bread")
        self.assertEqual(self.client.post(reverse("meals:weekly_skip", args=[WEEK.isoformat(), theirs.pk])).status_code, 404)

    def test_tick_and_never_a_staple(self):
        item = WeeklyItem.objects.create(household=home(), name="Pasta", category="pantry")
        household = home()
        household.pantry = "pasta"
        household.save()
        items = self._items()
        self.assertIn("Pasta", items)  # wanted every week, so not hidden under "Check at home"
        self.assertEqual(items["Pasta"].can_be_staple, "")
        ShoppingCheck.objects.create(household=home(), week=WEEK, key=f"weekly-{item.pk}")
        self.assertTrue(self._items()["Pasta"].checked)


class SettingsLinkTests(TestCase):
    def test_no_icon(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        page = self.client.get(reverse("meals:home"))
        self.assertContains(page, '">Settings</a>')
        self.assertNotContains(page, "⚙")


class MealPickerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com")
        self.client.force_login(self.user)
        self.ana = FamilyMember.objects.create(household=home(), name="Ana")
        self.lasagne = Dish.objects.create(household=home(), name="Lasagne", status="favourite", minutes=60)
        self.lasagne.ingredients.create(name="Mince", quantity=500, unit="g", category="meat")
        self.curry = Dish.objects.create(household=home(), name="Thai green curry", status="try", notes="from the neighbours")
        self.curry.ingredients.create(name="Coconut milk", quantity=1, unit="can", category="pantry")
        self.soup = Dish.objects.create(household=home(), name="Tomato soup")
        PlannedMeal.objects.create(household=home(), date=WEEK - timedelta(weeks=2), dish=self.soup)
        self.url = reverse("meals:meal_pick") + "?date=2026-10-07&next=/week/2026-10-05/"

    def test_day_add_opens_the_picker(self):
        page = self.client.get(reverse("meals:menu_of", args=["2026-10-05"]))
        self.assertContains(page, 'href="/meal/pick/?date=2026-10-07"')

    def test_lists_recipes_with_details_favourites_first(self):
        page = self.client.get(self.url)
        groups = [(title, [d.name for d in dishes]) for title, dishes in page.context["groups"]]
        self.assertEqual(groups, [("★ Favourites", ["Lasagne"]), ("Want to try", ["Thai green curry"]), ("Other dishes", ["Tomato soup"])])
        self.assertContains(page, "⏱ 60 min")
        self.assertContains(page, "Cooked 1×")
        self.assertContains(page, '<p class="small pick-ingredients">Mince</p>')
        self.assertContains(page, "<summary>More</summary>")
        # Search text covers name, ingredients and notes.
        self.assertContains(page, 'data-search="thai green curry from the neighbours  other coconut milk"')

    def test_server_side_search(self):
        for query, names in [("coconut", ["Thai green curry"]), ("neighbours", ["Thai green curry"]), ("mince lasagne", ["Lasagne"]), ("xyz", [])]:
            page = self.client.get(self.url + f"&q={query}")
            self.assertEqual([d.name for _, ds in page.context["groups"] for d in ds], names, query)
        self.assertContains(self.client.get(self.url + "&q=xyz"), 'id="pick-empty">')

    def test_pick_a_recipe(self):
        response = self.client.post(reverse("meals:meal_pick"), {"date": "2026-10-07", "slot": "dinner", "dish": self.lasagne.pk, "next": "/week/2026-10-05/"})
        self.assertRedirects(response, "/week/2026-10-05/")
        meal = PlannedMeal.objects.get(dish=self.lasagne)
        self.assertEqual((meal.date, meal.slot, list(meal.eaters.all())), (date(2026, 10, 7), "dinner", [self.ana]))

    def test_recipe_without_ingredients_continues_to_ingredients(self):
        response = self.client.post(reverse("meals:meal_pick"), {"date": "2026-10-07", "slot": "lunch", "dish": self.soup.pk, "next": "/"})
        self.assertRedirects(response, reverse("meals:ingredients", args=[self.soup.pk]) + "?next=%2F", fetch_redirect_response=False)

    def test_slot_defaults_to_the_free_meal(self):
        self.assertEqual(self.client.get(self.url).context["slot"], "dinner")
        PlannedMeal.objects.create(household=home(), date=date(2026, 10, 7), dish=self.soup)
        self.assertEqual(self.client.get(self.url).context["slot"], "lunch")

    def test_leftovers_from_earlier_this_week(self):
        cooked = PlannedMeal.objects.create(household=home(), date=date(2026, 10, 6), dish=self.lasagne, servings=8)
        PlannedMeal.objects.create(household=home(), date=date(2026, 10, 8), dish=self.curry)  # later: not offered
        page = self.client.get(self.url)
        self.assertEqual(list(page.context["leftovers"]), [cooked])
        self.client.post(reverse("meals:meal_pick"), {"date": "2026-10-07", "slot": "lunch", "leftover": cooked.pk})
        meal = PlannedMeal.objects.get(date=date(2026, 10, 7))
        self.assertEqual((meal.dish, meal.leftovers, meal.slot), (self.lasagne, True, "lunch"))

    def test_new_recipe_typed_in_is_planned_on_the_day(self):
        response = self.client.post(reverse("meals:meal_pick"), {"date": "2026-10-07", "slot": "lunch", "new": "1", "next": "/week/2026-10-05/"})
        self.assertRedirects(response, reverse("meals:recipe_add") + "?plan=2026-10-07%3Alunch&next=%2Fweek%2F2026-10-05%2F", fetch_redirect_response=False)
        page = self.client.get(response.url)
        self.assertContains(page, "It will be planned for Wednesday 7 Oct, lunch.")
        self.assertContains(page, 'href="/recipes/new/?plan=2026-10-07%3Alunch&amp;next=/week/2026-10-05/"')
        response = self.client.post("/recipes/new/?plan=2026-10-07%3Alunch&next=%2Fweek%2F2026-10-05%2F",
                                    {"name": "Pancakes", "kind": "other", "servings": "4", "status": "try"}, follow=True)
        dish = Dish.objects.get(name="Pancakes")
        meal = PlannedMeal.objects.get(dish=dish)
        self.assertEqual((meal.date, meal.slot), (date(2026, 10, 7), "lunch"))
        self.assertContains(response, "planned it for Wed lunch")
        self.assertEqual(response.request["QUERY_STRING"], "next=%2Fweek%2F2026-10-05%2F")  # ingredients, then back to the week


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini")
class PlannedRecipeFromLinkTests(TestCase):
    def setUp(self):
        for patcher in [
            mock.patch.object(recipe_import, "start", side_effect=lambda job: recipe_import.run(job.pk)),
            mock.patch.object(recipe_import, "fetch_page", return_value="<html><body>Pad thai</body></html>"),
            mock.patch.object(planner, "link_works", return_value=True),
            mock.patch.object(planner, "get_client", return_value=FakeOpenAI(reply(recipe_call(name="Pad thai")))),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        approve_ai()
        self.plan = {"plan": "2026-10-07:dinner", "next": "/week/2026-10-05/"}

    def test_link_recipe_is_planned_when_saved(self):
        self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/pad-thai", **self.plan})
        job = RecipeImport.objects.get()
        self.assertEqual((job.plan_date, job.plan_slot, job.plan_next), (date(2026, 10, 7), "dinner", "/week/2026-10-05/"))
        review = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(review, "Once saved, it's planned for Wednesday 7 Oct, dinner.")
        data = {"name": "Pad thai", "recipe_url": "https://example.com/pad-thai", "source": "", "kind": "meat", "minutes": "30",
                "servings": "4", "instructions": "", "notes": "", "status": "try",
                "ingredients-TOTAL_FORMS": "0", "ingredients-INITIAL_FORMS": "0",
                "ingredients-MIN_NUM_FORMS": "0", "ingredients-MAX_NUM_FORMS": "1000"}
        response = self.client.post(reverse("meals:recipe_import", args=[job.pk]), data)
        self.assertRedirects(response, "/week/2026-10-05/", fetch_redirect_response=False)
        meal = PlannedMeal.objects.get()
        self.assertEqual((meal.dish.name, meal.date, meal.slot), ("Pad thai", date(2026, 10, 7), "dinner"))

    def test_known_link_is_planned_straight_away(self):
        dish = Dish.objects.create(household=home(), name="Pad thai", recipe_url="https://example.com/pad-thai")
        response = self.client.post(reverse("meals:recipe_link_new"), {"url": "https://www.example.com/pad-thai/", **self.plan})
        self.assertRedirects(response, "/week/2026-10-05/", fetch_redirect_response=False)
        self.assertEqual(PlannedMeal.objects.get().dish, dish)
        self.assertFalse(RecipeImport.objects.exists())
