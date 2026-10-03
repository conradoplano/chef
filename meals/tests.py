from datetime import date, timedelta
from unittest import mock
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import shopping
from .models import Dish, ExtraItem, FamilyMember, Feedback, Household, PlannedMeal, Rule, ShoppingCheck


class WeekTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))

    def test_menu_shows_selected_week(self):
        response = self.client.get(reverse("meals:menu_of", args=["2026-10-01"]))
        self.assertContains(response, "28 Sep – 4 Oct 2026")
        self.assertContains(response, reverse("meals:menu_of", args=["2026-10-05"]))

    def test_shopping_shows_selected_week(self):
        response = self.client.get(reverse("meals:shopping_of", args=["2026-10-01"]))
        self.assertContains(response, "28 Sep – 4 Oct 2026")

    def test_invalid_date_is_404(self):
        response = self.client.get(reverse("meals:menu_of", args=["nope"]))
        self.assertEqual(response.status_code, 404)


class MenuTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com")
        self.client.force_login(self.user)
        self.curry = Dish.objects.create(
            name="Paneer curry", kind=Dish.Kind.VEGETARIAN, minutes=30, recipe_url="https://example.com/curry"
        )

    def _post(self, url, **data):
        payload = {"date": "2026-09-29", "slot": "dinner", "dish_name": "", "kind": "other",
                   "recipe_url": "", "minutes": "", "note": ""}
        payload.update(data)
        return self.client.post(url, payload)

    def test_week_lists_meals_in_order_and_only_that_week(self):
        PlannedMeal.objects.create(date=date(2026, 10, 3), slot="dinner", dish=self.curry)
        PlannedMeal.objects.create(date=date(2026, 10, 3), slot="lunch", dish=self.curry, leftovers=True)
        PlannedMeal.objects.create(date=date(2026, 10, 5), dish=Dish.objects.create(name="Next week pizza"))
        response = self.client.get(reverse("meals:menu_of", args=["2026-09-28"]))
        content = response.content.decode()
        self.assertLess(content.index("Leftover paneer curry"), content.index("🌱 Paneer curry"))
        self.assertContains(response, "https://example.com/curry")
        self.assertContains(response, "30 min")
        self.assertNotContains(response, "Next week pizza")
        self.assertEqual(response.context["meal_count"], 2)

    def test_add_meal_creates_new_dish(self):
        response = self._post(reverse("meals:meal_new"), dish_name="  Teriyaki   tofu ", kind="vegetarian",
                              minutes="25", recipe_url="bbcgoodfood.com/tofu")
        meal = PlannedMeal.objects.get()
        # A dish without ingredients continues to its ingredients, then back to the week.
        self.assertRedirects(
            response, reverse("meals:ingredients", args=[meal.dish.pk]) + "?next=%2Fweek%2F2026-09-29%2F",
            fetch_redirect_response=False,
        )
        self.assertEqual(meal.dish.name, "Teriyaki tofu")
        self.assertEqual(meal.dish.recipe_url, "https://bbcgoodfood.com/tofu")
        self.assertEqual(meal.updated_by, self.user)

    def test_add_meal_reuses_existing_dish_by_name(self):
        self._post(reverse("meals:meal_new"), dish_name="paneer CURRY", kind="vegetarian", minutes="35")
        self.assertEqual(Dish.objects.count(), 1)
        self.curry.refresh_from_db()
        self.assertEqual(self.curry.minutes, 35)
        self.assertEqual(PlannedMeal.objects.get().dish, self.curry)

    def test_new_meal_form_prefills_date_and_slot(self):
        response = self.client.get(reverse("meals:meal_new") + "?date=2026-10-03&slot=lunch")
        self.assertEqual(response.context["form"].initial["date"], date(2026, 10, 3))
        self.assertEqual(response.context["form"].initial["slot"], "lunch")
        self.assertContains(response, '<option value="Paneer curry">')

    def test_edit_meal_changes_dish(self):
        meal = PlannedMeal.objects.create(date=date(2026, 9, 29), dish=self.curry)
        response = self.client.get(reverse("meals:meal", args=[meal.pk]))
        self.assertContains(response, 'value="Paneer curry"')
        self._post(reverse("meals:meal", args=[meal.pk]), dish_name="Salmon pasta", kind="fish")
        meal.refresh_from_db()
        self.assertEqual(meal.dish.name, "Salmon pasta")
        self.assertTrue(Dish.objects.filter(name="Paneer curry").exists())

    def test_remove_meal(self):
        meal = PlannedMeal.objects.create(date=date(2026, 9, 29), dish=self.curry)
        response = self.client.post(reverse("meals:meal", args=[meal.pk]), {"delete": "1"})
        self.assertRedirects(response, reverse("meals:menu_of", args=["2026-09-29"]))
        self.assertFalse(PlannedMeal.objects.exists())

    def test_dish_name_required(self):
        response = self._post(reverse("meals:meal_new"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PlannedMeal.objects.exists())

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("meals:meal_new"))
        self.assertEqual(response.status_code, 302)


class FamilyTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))

    def test_empty_page_shows_hints(self):
        response = self.client.get(reverse("meals:family"))
        self.assertContains(response, "Add everyone who eats with us")
        self.assertContains(response, "No rules yet")

    def test_add_edit_and_remove_member(self):
        response = self.client.post(reverse("meals:member_new"), {
            "name": "Leo", "kind": "child", "birth_year": "2019", "likes": "Pizza", "allergies": "Peanuts",
        })
        self.assertRedirects(response, reverse("meals:family"))
        leo = FamilyMember.objects.get()
        page = self.client.get(reverse("meals:family"))
        self.assertContains(page, "Leo")
        self.assertContains(page, "⚠ Peanuts")
        self.assertContains(page, f"Child · {date.today().year - 2019}")

        self.client.post(reverse("meals:member", args=[leo.pk]), {"name": "Leo", "kind": "child", "dislikes": "Peppers"})
        leo.refresh_from_db()
        self.assertEqual(leo.dislikes, "Peppers")

        self.client.post(reverse("meals:member", args=[leo.pk]), {"delete": "1"})
        self.assertFalse(FamilyMember.objects.exists())

    def test_add_toggle_edit_delete_rule(self):
        self.client.post(reverse("meals:family"), {"rule-text": "Fish twice a week", "rule-active": "on"})
        rule = Rule.objects.get()
        self.assertTrue(rule.active)

        self.client.post(reverse("meals:rule_toggle", args=[rule.pk]))
        rule.refresh_from_db()
        self.assertFalse(rule.active)

        self.client.post(reverse("meals:rule", args=[rule.pk]), {"text": "Fish at least twice a week", "active": "on"})
        rule.refresh_from_db()
        self.assertEqual((rule.text, rule.active), ("Fish at least twice a week", True))

        self.client.post(reverse("meals:rule", args=[rule.pk]), {"delete": "1"})
        self.assertFalse(Rule.objects.exists())

    def test_empty_rule_is_rejected(self):
        response = self.client.post(reverse("meals:family"), {"rule-text": "", "rule-active": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Rule.objects.exists())

    def test_toggle_requires_post(self):
        rule = Rule.objects.create(text="Pizza on Sunday")
        self.assertEqual(self.client.get(reverse("meals:rule_toggle", args=[rule.pk])).status_code, 405)

    def test_household_edit(self):
        response = self.client.post(reverse("meals:household"), {
            "weekday_minutes": "30", "adventurousness": "6", "priority": "waste", "pantry": "Olive oil\nRice",
        })
        self.assertRedirects(response, reverse("meals:family"))
        household = Household.load()
        self.assertEqual((household.weekday_minutes, household.priority), (30, "waste"))
        page = self.client.get(reverse("meals:family"))
        self.assertContains(page, "30 min")
        self.assertContains(page, "6 / 10")
        self.assertContains(page, "Olive oil<br>Rice")

    def test_household_form_leaves_usual_week_alone(self):
        household = Household.load()
        household.usual_week = {"0": {"dinner": {"on": True, "eaters": []}}}
        household.save()
        response = self.client.get(reverse("meals:household"))
        self.assertNotContains(response, "usual_week")
        self.client.post(reverse("meals:household"), {"weekday_minutes": "25", "priority": "balanced"})
        household.refresh_from_db()
        self.assertEqual(household.weekday_minutes, 25)
        self.assertEqual(household.usual_week, {"0": {"dinner": {"on": True, "eaters": []}}})

    def test_adventurousness_out_of_range(self):
        response = self.client.post(reverse("meals:household"), {"adventurousness": "11", "priority": "balanced"})
        self.assertContains(response, "from 1 to 10")


class RecipeSourceTests(TestCase):
    def test_known_site_gets_friendly_name(self):
        self.assertEqual(Dish(recipe_url="https://www.bbcgoodfood.com/recipes/x").recipe_source, "BBC Good Food")
        self.assertEqual(Dish(recipe_url="https://cooking.nytimes.com/recipes/1").recipe_source, "NYT Cooking")

    def test_unknown_site_shows_domain(self):
        self.assertEqual(Dish(recipe_url="https://www.smittenkitchen.com/a").recipe_source, "smittenkitchen.com")

    def test_no_link(self):
        self.assertEqual(Dish(recipe_url="").recipe_source, "")


@override_settings(TIME_ZONE="Europe/Berlin")
class MenuCardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com")
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.dish = Dish.objects.create(name="Shepherd's pie", kind="meat", minutes=75,
                                        recipe_url="https://www.bbcgoodfood.com/recipes/pie")

    def _menu(self, day):
        return self.client.get(reverse("meals:menu_of", args=[day.isoformat()]))

    def test_card_shows_source_edit_and_add_separately(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self._menu(self.today)
        self.assertContains(response, "BBC Good Food ↗")
        self.assertContains(response, "⏱ 75 min")
        self.assertContains(response, f'href="{reverse("meals:meal", args=[meal.pk])}" aria-label="Change this meal"')
        self.assertContains(response, "+ Add", count=7)

    def test_card_top_links_to_recipe(self):
        PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self._menu(self.today)
        self.assertContains(
            response,
            '<a class="meal-link" href="https://www.bbcgoodfood.com/recipes/pie" target="_blank" rel="noopener">'
            "🥩 Shepherd&#x27;s pie</a>",
            html=False,
        )

    def test_no_card_link_without_recipe(self):
        PlannedMeal.objects.create(date=self.today, dish=Dish.objects.create(name="Pizza"))
        self.assertNotContains(self._menu(self.today), "meal-link")

    def test_leftovers_show_no_cooking_instead_of_time(self):
        PlannedMeal.objects.create(date=self.today, slot="lunch", dish=self.dish, leftovers=True)
        response = self._menu(self.today)
        self.assertContains(response, "Leftovers – no cooking needed")
        self.assertNotContains(response, "75 min")
        # Leftovers still link to the recipe.
        self.assertContains(response, "BBC Good Food ↗")
        self.assertContains(
            response,
            '<a class="meal-link" href="https://www.bbcgoodfood.com/recipes/pie" target="_blank" rel="noopener">'
            "🥩 Leftover shepherd&#x27;s pie</a>",
        )

    def test_feedback_button_only_for_eaten_cooked_meals(self):
        past = PlannedMeal.objects.create(date=self.today - timedelta(days=1), dish=self.dish)
        PlannedMeal.objects.create(date=self.today - timedelta(days=1), slot="lunch", dish=self.dish, leftovers=True)
        future = PlannedMeal.objects.create(date=self.today + timedelta(days=1), dish=self.dish)
        start = self.today - timedelta(days=self.today.weekday())
        content = "".join(self._menu(d).content.decode() for d in {start, past.date, future.date})
        self.assertIn(reverse("meals:feedback", args=[past.pk]), content)
        self.assertNotIn(reverse("meals:feedback", args=[future.pk]), content)
        self.assertEqual(content.count("/feedback/"), content.count(reverse("meals:feedback", args=[past.pk])))

    def test_give_edit_and_remove_feedback(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        url = reverse("meals:feedback", args=[meal.pk])
        response = self.client.post(url, {"kids": "loved", "parents": "liked", "reaction": "Leo: tummy ache", "notes": ""})
        self.assertRedirects(response, reverse("meals:menu_of", args=[self.today.isoformat()]))
        review = Feedback.objects.get()
        self.assertEqual((review.kids, review.parents, review.updated_by), ("loved", "liked", self.user))

        page = self._menu(self.today)
        self.assertContains(page, "Kids ❤️")
        self.assertContains(page, "Parents 👍")
        self.assertContains(page, 'title="Leo: tummy ache">⚠</span>')
        self.assertContains(page, ">Feedback</a>")
        # Kids and parents disagree, so no shortcut is highlighted.
        self.assertNotContains(page, 'aria-pressed="true"')

        self.client.post(url, {"kids": "", "parents": "okay", "reaction": "", "notes": "A bit dry"})
        review.refresh_from_db()
        self.assertEqual((review.kids, review.parents, review.notes), ("", "okay", "A bit dry"))
        self.assertEqual(Feedback.objects.count(), 1)

        self.client.post(url, {"delete": "1"})
        self.assertFalse(Feedback.objects.exists())

    def test_feedback_kept_with_meal_and_removed_with_it(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        Feedback.objects.create(meal=meal, kids="disliked")
        self.client.post(reverse("meals:meal", args=[meal.pk]), {"delete": "1"})
        self.assertFalse(Feedback.objects.exists())


class HomeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com", name="Pat Parent")
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.dish = Dish.objects.create(name="Salmon pasta", kind="fish", minutes=20)

    def test_shows_todays_meals_with_cards(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self.client.get(reverse("meals:home"))
        self.assertContains(response, ", Pat")
        self.assertContains(response, "🐟 Salmon pasta")
        self.assertContains(response, "⏱ 20 min")
        self.assertContains(response, reverse("meals:feedback_quick", args=[meal.pk]) + "?next=/")
        # Card links bring you back to the home page.
        self.assertContains(response, f'{reverse("meals:meal", args=[meal.pk])}?next=/"')
        self.assertContains(response, f'{reverse("meals:feedback", args=[meal.pk])}?next=/"')

    def test_empty_day(self):
        response = self.client.get(reverse("meals:home"))
        self.assertContains(response, "Nothing planned for today.")
        self.assertContains(response, reverse("meals:menu"))

    def test_week_cards_have_no_return_link(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self.client.get(reverse("meals:menu"))
        self.assertContains(response, f'href="{reverse("meals:meal", args=[meal.pk])}" ')
        self.assertNotContains(response, "?next=")

    def test_edit_and_feedback_return_home(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self.client.post(reverse("meals:feedback", args=[meal.pk]) + "?next=/", {"kids": "loved"})
        self.assertRedirects(response, "/")
        response = self.client.post(reverse("meals:meal", args=[meal.pk]) + "?next=/", {
            "date": self.today.isoformat(), "slot": "dinner", "dish_name": "Salmon pasta", "kind": "fish",
        })
        self.assertRedirects(response, "/")

    def test_unsafe_next_is_ignored(self):
        meal = PlannedMeal.objects.create(date=self.today, dish=self.dish)
        response = self.client.post(reverse("meals:feedback", args=[meal.pk]) + "?next=https://evil.example/", {"notes": "Fine"})
        self.assertRedirects(response, reverse("meals:menu_of", args=[self.today.isoformat()]))


class ShoppingListTests(TestCase):
    """Week of Mon 28 Sep 2026."""

    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com", name="Pat Parent")
        self.client.force_login(self.user)
        self.week = date(2026, 9, 28)
        self.curry = Dish.objects.create(name="Paneer curry", recipe_url="https://www.bbcgoodfood.com/curry")
        self.curry.ingredients.create(name="Onions", quantity=2, unit="pcs", category="vegetables")
        self.curry.ingredients.create(
            name="Spinach", quantity=300, unit="g", category="vegetables", note="fresh or frozen"
        )
        self.curry.ingredients.create(name="Garlic", quantity=None, category="vegetables")
        self.curry.ingredients.create(name="Rice", quantity=250, unit="g", category="pantry")
        self.pie = Dish.objects.create(name="Shepherd's pie")
        self.pie.ingredients.create(name="onion", quantity=1, unit="pcs", category="vegetables")
        self.pie.ingredients.create(name="Potatoes", quantity=Decimal("1.2"), unit="kg", category="vegetables")
        self.pie.ingredients.create(name="Lamb mince", quantity=500, unit="g", category="meat")
        self.pie.ingredients.create(name="Garlic", quantity=2, unit="clove", category="other")
        PlannedMeal.objects.create(date=date(2026, 9, 29), dish=self.curry)
        PlannedMeal.objects.create(date=date(2026, 10, 3), dish=self.pie)
        PlannedMeal.objects.create(date=date(2026, 10, 4), slot="lunch", dish=self.pie, leftovers=True)

    def _build(self):
        sections, at_home, missing = shopping.build(self.week)
        return {i.name: i for s in sections for i in s.items}, sections, at_home, missing

    def test_merges_ingredients_across_dishes(self):
        items, sections, _, _ = self._build()
        self.assertEqual(items["Onions"].quantity, "3 pcs")
        self.assertEqual([u.label for u in items["Onions"].uses], ["Tue · Paneer curry", "Sat · Shepherd's pie"])
        # Unknown quantity in one recipe is flagged; the category comes from the recipe that has one.
        self.assertEqual(items["Garlic"].quantity, "2 clove + more")
        self.assertEqual(items["Garlic"].category, "vegetables")
        self.assertEqual(items["Spinach"].notes, ["fresh or frozen"])
        self.assertEqual([s.category for s in sections], ["vegetables", "meat", "pantry"])

    def test_converts_units(self):
        self.curry.ingredients.create(name="Potato", quantity=300, unit="g", category="vegetables")
        items, *_ = self._build()
        # The line takes its name from the first meal of the week that uses it.
        self.assertEqual(items["Potato"].quantity, "1.5 kg")
        self.assertEqual(shopping.format_quantities({"g": Decimal(750)}, False), "750 g")
        self.assertEqual(shopping.format_quantities({"ml": Decimal(1500)}, False), "1.5 l")

    def test_leftovers_add_nothing(self):
        items, *_ = self._build()
        self.assertEqual(items["Lamb mince"].quantity, "500 g")
        self.assertEqual(len(items["Lamb mince"].uses), 1)

    def test_other_weeks_are_separate(self):
        sections, *_ = shopping.build(date(2026, 10, 5))
        self.assertEqual(sections, [])

    def test_pantry_staples_go_to_check_at_home(self):
        household = Household.load()
        household.pantry = "rice, olive oil\nGarlic"
        household.save()
        items, _, at_home, _ = self._build()
        self.assertEqual([i.name for i in at_home], ["Garlic", "Rice"])
        self.assertNotIn("Rice", items)

    def test_meals_without_ingredients_are_reported(self):
        PlannedMeal.objects.create(date=date(2026, 10, 2), dish=Dish.objects.create(name="Flatbreads"))
        *_, missing = self._build()
        self.assertEqual([m.dish.name for m in missing], ["Flatbreads"])
        response = self.client.get(reverse("meals:shopping_of", args=["2026-09-28"]))
        self.assertContains(response, "No ingredients yet")

    def test_page_shows_sections_quantities_and_recipe_links(self):
        response = self.client.get(reverse("meals:shopping_of", args=["2026-09-30"]))
        self.assertContains(response, "🥕 Fruit &amp; vegetables")
        self.assertContains(response, '<span class="qty">3 pcs</span>')
        self.assertContains(
            response,
            'href="https://www.bbcgoodfood.com/curry" target="_blank" rel="noopener" title="Tue · Paneer curry">'
            '<span class="use-name">Tue · Paneer curry</span> ↗</a>',
        )
        self.assertContains(
            response,
            '<span class="badge use" title="Sat · Shepherd&#x27;s pie"><span class="use-name">Sat · Shepherd&#x27;s pie</span></span>',
        )
        self.assertContains(response, 'id="hide-uses"')
        self.assertContains(response, "fresh or frozen")
        self.assertContains(response, "0</strong> of 6 bought")

    def test_toggle_marks_bought_for_everyone(self):
        url = reverse("meals:shopping_toggle", args=["2026-09-28"])
        response = self.client.post(url, {"key": "onion", "checked": "1"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json()["checked"], {"onion": "Pat"})
        # Someone else sees it on their next poll.
        other = User.objects.create_user(email="other@example.com")
        self.client.force_login(other)
        state = self.client.get(reverse("meals:shopping_state", args=["2026-09-28"])).json()
        self.assertEqual(state["checked"], {"onion": "Pat"})
        page = self.client.get(reverse("meals:shopping_of", args=["2026-09-28"]))
        self.assertContains(page, "✓ Pat")
        self.assertContains(page, "1</strong> of 6 bought")

        # Setting the same state twice doesn't flip it back.
        self.client.post(url, {"key": "onion", "checked": "1"})
        self.assertEqual(ShoppingCheck.objects.get().checked_by, other)
        self.client.post(url, {"key": "onion", "checked": "0"})
        self.assertFalse(ShoppingCheck.objects.exists())

    def test_signature_changes_when_menu_changes(self):
        state_url = reverse("meals:shopping_state", args=["2026-09-28"])
        before = self.client.get(state_url).json()["signature"]
        self.assertEqual(self.client.get(state_url).json()["signature"], before)
        PlannedMeal.objects.filter(dish=self.curry).delete()
        self.assertNotEqual(self.client.get(state_url).json()["signature"], before)

    def test_extra_items(self):
        self.client.post(
            reverse("meals:extra_add", args=["2026-10-01"]),
            {"extra-name": "Milk", "extra-quantity": "2 l", "extra-category": "dairy"},
        )
        extra = ExtraItem.objects.get()
        self.assertEqual((extra.week, extra.created_by), (self.week, self.user))
        page = self.client.get(reverse("meals:shopping_of", args=["2026-09-28"]))
        self.assertContains(page, "🧀 Dairy &amp; eggs")
        self.assertContains(page, '<span class="qty">2 l</span>')
        self.client.post(
            reverse("meals:shopping_toggle", args=["2026-09-28"]), {"key": f"extra-{extra.pk}", "checked": "1"}
        )
        response = self.client.post(reverse("meals:extra_delete", args=[extra.pk]))
        self.assertRedirects(response, reverse("meals:shopping_of", args=["2026-09-28"]) + f"?removed={extra.pk}")
        self.assertNotIn("Milk", [i.name for s in shopping.build(self.week)[0] for i in s.items])

    def test_undo_remove_extra_item(self):
        extra = ExtraItem.objects.create(week=self.week, name="Milk", category="dairy")
        toggle = reverse("meals:shopping_toggle", args=["2026-09-28"])
        self.client.post(toggle, {"key": f"extra-{extra.pk}", "checked": "1"})
        response = self.client.post(reverse("meals:extra_delete", args=[extra.pk]), follow=True)
        self.assertContains(response, "Removed <strong>Milk</strong>.")
        self.assertContains(response, reverse("meals:extra_restore", args=[extra.pk]))
        self.assertNotContains(response, 'data-key="extra-')

        response = self.client.post(reverse("meals:extra_restore", args=[extra.pk]), follow=True)
        self.assertContains(response, "Milk is back on the list.")
        # Back exactly as it was, including the tick.
        self.assertContains(response, f'data-key="extra-{extra.pk}"')
        self.assertContains(response, "✓ Pat")

    def test_removed_items_are_purged_after_a_day(self):
        old = ExtraItem.objects.create(
            week=self.week, name="Old", removed_at=timezone.now() - timedelta(days=2)
        )
        ShoppingCheck.objects.create(week=self.week, key=f"extra-{old.pk}")
        extra = ExtraItem.objects.create(week=self.week, name="Bread")
        self.client.post(reverse("meals:extra_delete", args=[extra.pk]))
        self.assertEqual(list(ExtraItem.objects.values_list("name", flat=True)), ["Bread"])
        self.assertFalse(ShoppingCheck.objects.exists())

    def test_removed_banner_only_for_removed_items(self):
        extra = ExtraItem.objects.create(week=self.week, name="Eggs")
        response = self.client.get(reverse("meals:shopping_of", args=["2026-09-28"]) + f"?removed={extra.pk}")
        self.assertNotContains(response, "Removed <strong>")
        response = self.client.get(reverse("meals:shopping_of", args=["2026-09-28"]) + "?removed=abc")
        self.assertEqual(response.status_code, 200)

    def test_edit_ingredients(self):
        url = reverse("meals:ingredients", args=[self.pie.pk]) + "?next=/week/2026-09-28/"
        response = self.client.get(url)
        self.assertContains(response, "Lamb mince")
        data = {
            "ingredients-TOTAL_FORMS": "5", "ingredients-INITIAL_FORMS": "4",
            "ingredients-MIN_NUM_FORMS": "0", "ingredients-MAX_NUM_FORMS": "1000", "dish-servings": "4",
        }
        for i, ing in enumerate(self.pie.ingredients.all()):
            data.update({
                f"ingredients-{i}-id": ing.pk, f"ingredients-{i}-name": ing.name,
                f"ingredients-{i}-quantity": ing.quantity or "", f"ingredients-{i}-unit": ing.unit,
                f"ingredients-{i}-category": ing.category, f"ingredients-{i}-note": "",
            })
        data["ingredients-3-DELETE"] = "on"
        data.update({
            "ingredients-4-name": "Peas", "ingredients-4-quantity": "200", "ingredients-4-unit": "g",
            "ingredients-4-category": "frozen", "ingredients-4-note": "",
        })
        response = self.client.post(url, data)
        self.assertRedirects(response, "/week/2026-09-28/")
        self.assertEqual(
            sorted(self.pie.ingredients.values_list("name", flat=True), key=str.lower),
            ["Lamb mince", "onion", "Peas", "Potatoes"],
        )


class QuickFeedbackTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        self.today = timezone.localdate()
        self.meal = PlannedMeal.objects.create(date=self.today, dish=Dish.objects.create(name="Tacos"))
        self.url = reverse("meals:feedback_quick", args=[self.meal.pk])

    def _menu(self):
        return self.client.get(reverse("meals:menu_of", args=[self.today.isoformat()]))

    def test_card_shows_four_shortcuts(self):
        response = self._menu()
        for emoji in ["❤️", "👍", "😐", "👎"]:
            self.assertContains(response, f">{emoji}</button>")
        self.assertContains(response, 'title="Loved it – everyone"')

    def test_sets_same_rating_for_kids_and_parents(self):
        response = self.client.post(self.url, {"rating": "loved"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json(), {"rating": "loved"})
        review = Feedback.objects.get()
        self.assertEqual((review.kids, review.parents), ("loved", "loved"))
        self.assertContains(self._menu(), 'value="loved" class="qf" aria-pressed="true"')

    def test_changing_keeps_reaction_and_notes(self):
        Feedback.objects.create(meal=self.meal, kids="loved", parents="okay", reaction="Rash", notes="Less chili")
        self.client.post(self.url, {"rating": "liked"})
        review = Feedback.objects.get()
        self.assertEqual((review.kids, review.parents, review.reaction, review.notes), ("liked", "liked", "Rash", "Less chili"))

    def test_tapping_again_clears(self):
        self.client.post(self.url, {"rating": "okay"})
        response = self.client.post(self.url, {"rating": "okay"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json(), {"rating": ""})
        self.assertFalse(Feedback.objects.exists())

    def test_clearing_keeps_feedback_with_notes(self):
        Feedback.objects.create(meal=self.meal, kids="okay", parents="okay", notes="Too salty")
        self.client.post(self.url, {"rating": "okay"})
        review = Feedback.objects.get()
        self.assertEqual((review.kids, review.parents, review.notes), ("", "", "Too salty"))

    def test_without_javascript_returns_to_the_card(self):
        response = self.client.post(self.url + "?next=/", {"rating": "disliked"})
        self.assertRedirects(response, f"/#meal-{self.meal.pk}", fetch_redirect_response=False)
        response = self.client.post(self.url, {"rating": "disliked"})
        self.assertRedirects(
            response, reverse("meals:menu_of", args=[self.today.isoformat()]) + f"#meal-{self.meal.pk}",
            fetch_redirect_response=False,
        )

    def test_unknown_rating(self):
        self.assertEqual(self.client.post(self.url, {"rating": "meh"}).status_code, 404)


class HomeRestOfWeekTests(TestCase):
    """Home page on Wednesday 30 Sep 2026 (week of Mon 28 Sep)."""

    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        dish = lambda name: Dish.objects.create(name=name)
        PlannedMeal.objects.create(date=date(2026, 9, 29), dish=dish("Yesterday soup"))
        PlannedMeal.objects.create(date=date(2026, 9, 30), dish=dish("Today tofu"))
        PlannedMeal.objects.create(date=date(2026, 10, 1), dish=dish("Thursday salmon"))
        PlannedMeal.objects.create(date=date(2026, 10, 4), slot="lunch", dish=dish("Sunday pie"), leftovers=True)
        PlannedMeal.objects.create(date=date(2026, 10, 5), dish=dish("Next week pizza"))

    def _home(self, today):
        with mock.patch("django.utils.timezone.localdate", return_value=today):
            return self.client.get(reverse("meals:home"))

    def test_shows_remaining_days_after_today(self):
        response = self._home(date(2026, 9, 30))
        content = response.content.decode()
        self.assertEqual([d["date"] for d in response.context["rest_of_week"]],
                         [date(2026, 10, d) for d in (1, 2, 3, 4)])
        self.assertLess(content.index("Today tofu"), content.index("Rest of the week"))
        self.assertLess(content.index("Rest of the week"), content.index("Thursday salmon"))
        self.assertIn("Leftover sunday pie", content)
        self.assertNotIn("Yesterday soup", content)
        self.assertNotIn("Next week pizza", content)
        # Empty days can still get a meal, and come back home afterwards.
        self.assertIn('href="/meal/new/?date=2026-10-02&amp;next=/"', content)

    def test_cards_in_rest_of_week_return_home(self):
        thursday = PlannedMeal.objects.get(dish__name="Thursday salmon")
        response = self._home(date(2026, 9, 30))
        self.assertContains(response, f'{reverse("meals:meal", args=[thursday.pk])}?next=/"')

    def test_sunday_links_to_next_week(self):
        response = self._home(date(2026, 10, 4))
        self.assertEqual(response.context["rest_of_week"], [])
        self.assertNotContains(response, "Rest of the week")
        self.assertContains(response, reverse("meals:menu_of", args=["2026-10-05"]))


class SelectedWeekTests(TestCase):
    """The week chosen on Menu or Shopping is kept while moving around the app."""

    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))

    def _get(self, name, *args, today=date(2026, 10, 3)):
        with mock.patch("django.utils.timezone.localdate", return_value=today):
            return self.client.get(reverse(name, args=args))

    def test_menu_week_is_kept_for_shopping_and_back(self):
        self._get("meals:menu_of", "2026-10-14")
        response = self._get("meals:shopping")
        self.assertEqual(response.context["start"], date(2026, 10, 12))
        self._get("meals:family")
        self.assertEqual(self._get("meals:menu").context["start"], date(2026, 10, 12))

    def test_shopping_week_is_kept_for_menu(self):
        self._get("meals:shopping_of", "2026-09-21")
        self.assertEqual(self._get("meals:menu").context["start"], date(2026, 9, 21))

    def test_back_to_this_week(self):
        response = self._get("meals:menu_of", "2026-10-14")
        self.assertContains(response, f'href="{reverse("meals:menu_of", args=["2026-09-28"])}">Back to this week')
        self._get("meals:menu_of", "2026-09-28")
        self.assertEqual(self._get("meals:shopping").context["start"], date(2026, 9, 28))

    def test_forgotten_the_next_day(self):
        self._get("meals:menu_of", "2026-10-14")
        response = self._get("meals:shopping", today=date(2026, 10, 4))
        self.assertEqual(response.context["start"], date(2026, 9, 28))

    def test_without_a_selection_shows_the_current_week(self):
        self.assertEqual(self._get("meals:menu").context["start"], date(2026, 9, 28))
