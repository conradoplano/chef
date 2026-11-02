"""Shopping for a few days only: from today until a chosen day, also into the next week."""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .models import Dish, ExtraItem, PlannedMeal, ShoppingCheck, WeeklyItem
from .testing import freeze_now, home

SATURDAY = date(2026, 10, 10)
WEDNESDAY = date(2026, 10, 14)
TRIP = reverse("meals:shopping") + "?until=2026-10-14"


class ShoppingTripTests(TestCase):
    def setUp(self):
        freeze_now(self, SATURDAY)
        self.client.force_login(User.objects.create_user(email="pat@example.com", name="Pat"))
        h = home()
        for day, name, ingredient in [(SATURDAY - timedelta(days=2), "Soup", "Leeks"), (SATURDAY, "Pie", "Lamb mince"),
                                      (date(2026, 10, 12), "Curry", "Rice"), (date(2026, 10, 15), "Tacos", "Tortillas")]:
            dish = Dish.objects.create(household=h, name=name)
            dish.ingredients.create(name=ingredient, quantity=500, unit="g", category="other")
            PlannedMeal.objects.create(household=h, date=day, dish=dish)
        ExtraItem.objects.create(household=h, week=date(2026, 10, 5), name="Milk")
        ExtraItem.objects.create(household=h, week=date(2026, 10, 12), name="Bread")
        WeeklyItem.objects.create(household=h, name="Fruit")
        WeeklyItem.objects.filter(name="Fruit").update(created_at=timezone.now() - timedelta(days=30))

    def names(self, response):
        return sorted(i.name for s in response.context["sections"] for i in s.items)

    def test_offered_on_this_weeks_list(self):
        page = self.client.get(reverse("meals:shopping"))
        self.assertContains(page, '<option value="">the whole week</option>')
        self.assertContains(page, '<option value="2026-10-10">today only</option>')
        self.assertContains(page, '<option value="2026-10-11">today – tomorrow</option>')
        self.assertContains(page, '<option value="2026-10-14">today – Wed 14 Oct</option>')
        self.assertNotContains(self.client.get(reverse("meals:shopping_of", args=["2026-10-19"])), "Shop for")

    def test_only_the_meals_until_that_day(self):
        page = self.client.get(TRIP)
        # Saturday's pie and Monday's curry (next week); not Thursday's tacos, nor Thursday's soup before today.
        self.assertEqual(self.names(page), ["Bread", "Fruit", "Lamb mince", "Milk", "Rice"])
        self.assertContains(page, "<strong>Sat 10 Oct – Wed 14 Oct</strong>")
        self.assertContains(page, '<option value="2026-10-14" selected>')
        self.assertContains(page, 'data-state-url="/shopping/trip/2026-10-10/2026-10-14/state/"')
        # The whole week again.
        self.assertEqual(self.names(self.client.get(reverse("meals:shopping") + "?until=")),
                         ["Fruit", "Lamb mince", "Leeks", "Milk"])

    def test_remembered_for_the_day(self):
        self.client.get(TRIP)
        self.assertTrue(self.client.get(reverse("meals:shopping")).context["trip"])
        self.client.get(reverse("meals:shopping_of", args=["2026-10-05"]))  # opening a week ends it
        self.assertNotIn("trip", self.client.get(reverse("meals:shopping")).context)

    def test_own_ticks(self):
        toggle = reverse("meals:shopping_trip_toggle", args=["2026-10-10", "2026-10-14"])
        state = self.client.post(toggle, {"key": "rice", "checked": "1"}, HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertEqual(state["checked"], {"rice": "Pat"})
        check = ShoppingCheck.objects.get()
        self.assertEqual((check.week, check.trip), (SATURDAY, True))
        # The week's list keeps its own ticks.
        self.client.post(reverse("meals:shopping_toggle", args=["2026-10-05"]), {"key": "lamb mince", "checked": "1"})
        week_state = self.client.get(reverse("meals:shopping_state", args=["2026-10-05"])).json()
        self.assertEqual(week_state["checked"], {"lamb mince": "Pat"})
        self.assertEqual(self.client.get(TRIP).context["bought"], 1)

    def test_changes_come_back_to_the_trip(self):
        response = self.client.post(reverse("meals:extra_add", args=["2026-10-10"]),
                                    {"extra-name": "Eggs", "extra-category": "dairy", "next": TRIP})
        self.assertRedirects(response, TRIP, fetch_redirect_response=False)
        eggs = ExtraItem.objects.get(name="Eggs")
        self.assertEqual(eggs.week, date(2026, 10, 5))  # this week's list
        response = self.client.post(reverse("meals:extra_delete", args=[eggs.pk]), {"next": TRIP})
        self.assertRedirects(response, f"{TRIP}&removed={eggs.pk}", fetch_redirect_response=False)
        page = self.client.get(TRIP)
        self.assertContains(page, f'<input type="hidden" name="next" value="{TRIP}">')

    def test_not_a_trip(self):
        for until in ["2026-10-09", "2026-10-24", "nonsense"]:  # yesterday, too far ahead
            self.assertNotIn("trip", self.client.get(reverse("meals:shopping") + f"?until={until}").context, until)
        self.assertEqual(self.client.get(reverse("meals:shopping_trip_state", args=["2026-10-14", "2026-10-10"])).status_code, 404)

    def test_shows_what_the_rest_of_the_week_also_needs(self):
        pie, tacos = Dish.objects.get(name="Pie"), Dish.objects.get(name="Tacos")
        pie.ingredients.create(name="Garlic", quantity=2, unit="clove", category="vegetables")
        tacos.ingredients.create(name="garlic", quantity=3, unit="clove", category="vegetables")
        Dish.objects.get(name="Curry").ingredients.create(name="Garlic", quantity=1, unit="clove", category="vegetables")
        page = self.client.get(TRIP)
        items = {i.name: i for s in page.context["sections"] for i in s.items}
        # Saturday's and Monday's garlic now; Thursday's tacos (after Wednesday, same week) need 3 more.
        self.assertEqual(items["Garlic"].quantity, "3 clove")
        self.assertContains(page, '<span class="later small">+ 3 clove later this week (Thu)</span>')
        self.assertIsNone(items["Rice"].later)
        # The whole week's list doesn't need it.
        self.assertNotContains(self.client.get(reverse("meals:shopping") + "?until="), "later this week")
