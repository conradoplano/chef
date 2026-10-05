"""Creating a week's menu with AI, against a fake OpenAI client."""
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import planner, schedule, shopping
from .models import Dish, FamilyMember, Feedback, MenuRequest, PlannedMeal, Rule
from .testing import approve_ai, home

WEEK = date(2026, 10, 5)  # Monday


def tool_call(data):
    return SimpleNamespace(type="function_call", name="save_menu", arguments=json.dumps(data), call_id="call_1")


def message(text="", refusal=False):
    part = SimpleNamespace(type="refusal", refusal=text) if refusal else SimpleNamespace(type="output_text", text=text)
    return SimpleNamespace(type="message", content=[part])


def reply(*items, status="completed", reason=None):
    return SimpleNamespace(
        id=f"resp_{len(items)}_{status}", status=status, output=list(items),
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


class FakeOpenAI:
    """Stands in for openai.OpenAI: replays the given responses and records the requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.timeouts = []
        self.responses = SimpleNamespace(create=self.create)

    def with_options(self, timeout=None):
        self.timeouts.append(timeout)
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def meal(day, slot, name, ingredients=(), leftovers=False, servings=4, kind="vegetarian", url="https://www.bbcgoodfood.com/x"):
    return {
        "date": day, "slot": slot, "dish_name": name, "kind": kind, "minutes": 0 if leftovers else 30,
        "recipe_url": "" if leftovers else url, "leftovers": leftovers, "servings": 0 if leftovers else servings,
        "note": "Make extra for Saturday" if not leftovers else "",
        "ingredients": [] if leftovers else [
            {"name": n, "quantity": q, "unit": u, "category": c, "note": ""} for n, q, u, c in ingredients
        ],
    }


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-6.1-sol", AI_EFFORT="medium", TIME_ZONE="Europe/Berlin")
class CreateMenuTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com", name="Pat Parent")
        approve_ai()
        self.client.force_login(self.user)
        self.mum = FamilyMember.objects.create(household=home(), name="Ana", kind="adult", birth_year=1985, allergies="Peanuts")
        self.kid = FamilyMember.objects.create(household=home(), name="Leo", kind="child", birth_year=2019, dislikes="Cooked peppers")
        # Run "background" work right away in tests, without checking links on the internet.
        patcher = mock.patch.object(planner, "start", side_effect=lambda request: planner.run(request.pk))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(planner, "link_works", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake(self, *replies):
        fake = FakeOpenAI(*replies)
        patcher = mock.patch.object(planner, "get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _post(self, slots, details="", replace=False):
        data = {"details": details}
        if replace:
            data["replace"] = "on"
        for day, slot, eaters in slots:
            data[f"{day}-{slot}-on"] = "on"
            data[f"{day}-{slot}-eaters"] = [str(m.pk) for m in eaters]
        return self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), data)

    def test_form_is_prefilled_from_usual_week(self):
        week = schedule.usual_week(home(), [self.mum, self.kid])
        week[0]["dinner"]["eaters"] = [self.mum.pk]  # Monday dinner: only Ana
        schedule.save_usual_week(home(), week)
        response = self.client.get(reverse("meals:menu_create", args=["2026-10-07"]))
        rows = response.context["rows"]
        self.assertEqual([r["key"] for r in rows][0], "2026-10-05")
        monday_dinner = rows[0]["slots"][1]
        self.assertTrue(monday_dinner["on"])
        self.assertEqual([e["eats"] for e in monday_dinner["members"]], [True, False])
        self.assertFalse(rows[0]["slots"][0]["on"])  # no lunch on weekdays by default
        self.assertTrue(rows[5]["slots"][0]["on"])  # Saturday lunch
        self.assertContains(response, 'name="2026-10-05-dinner-eaters" value="%d" checked' % self.mum.pk)

    def test_creates_menu_dishes_ingredients_and_shopping_list(self):
        fake = self._fake(reply(tool_call({
            "summary": "A relaxed week with two fish dishes.",
            "meals": [
                meal("2026-10-05", "dinner", "Paneer curry", [("Onions", 2, "pcs", "vegetables"), ("Paneer", 250, "g", "vegetarian")], servings=4),
                meal("2026-10-10", "lunch", "Paneer curry", leftovers=True),
            ],
        })))
        response = self._post([("2026-10-05", "dinner", [self.mum, self.kid]), ("2026-10-10", "lunch", [self.kid])],
                              details="Guests on Saturday")
        request = MenuRequest.objects.get()
        self.assertRedirects(response, reverse("meals:menu_request", args=[request.pk]), target_status_code=302)
        self.assertEqual(request.status, "done", request.error)
        self.assertEqual((request.input_tokens, request.output_tokens), (1000, 500))

        dinner = PlannedMeal.objects.get(date="2026-10-05")
        self.assertEqual(dinner.dish.name, "Paneer curry")
        self.assertEqual(dinner.dish.recipe_url, "https://www.bbcgoodfood.com/x")
        self.assertEqual(dinner.servings, 4)
        self.assertEqual(set(dinner.eaters.all()), {self.mum, self.kid})
        self.assertEqual(dinner.dish.ingredients.count(), 2)
        lunch = PlannedMeal.objects.get(date="2026-10-10")
        self.assertTrue(lunch.leftovers)
        self.assertEqual(list(lunch.eaters.all()), [self.kid])

        items = {i.name: i for s in shopping.build(home(), WEEK)[0] for i in s.items}
        self.assertEqual(items["Onions"].quantity, "2 pcs")

        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        self.assertContains(page, "A relaxed week with two fish dishes.")
        self.assertContains(page, "👥 4")

        # What was sent to OpenAI.
        sent = fake.requests[0]
        self.assertEqual(sent["model"], "gpt-6.1-sol")
        self.assertEqual(sent["reasoning"], {"effort": "medium"})
        self.assertEqual([t["type"] for t in sent["tools"]], ["web_search", "function"])
        self.assertEqual(sent["tools"][0]["user_location"], {"type": "approximate", "timezone": "Europe/Berlin"})
        self.assertEqual((sent["tools"][1]["name"], sent["tools"][1]["strict"]), ("save_menu", True))
        self.assertNotIn("previous_response_id", sent)
        self.assertIn("Plan exactly the meals", sent["instructions"])
        prompt = sent["input"][0]["content"]
        self.assertIn("ALLERGIES/INTOLERANCES: Peanuts", prompt)
        self.assertIn("Monday 2026-10-05 dinner: 2 eating - Ana (adult), Leo (child)", prompt)
        self.assertIn("Saturday 2026-10-10 lunch: 1 eating - Leo (child)", prompt)
        self.assertIn("Guests on Saturday", prompt)

    def test_prompt_includes_rules_household_and_feedback(self):
        Rule.objects.create(household=home(), text="Fish twice a week")
        Rule.objects.create(household=home(), text="Old rule", active=False)
        household = home()
        household.weekday_minutes = 30
        household.save()
        past = PlannedMeal.objects.create(household=home(), date=WEEK - timedelta(days=3), dish=Dish.objects.create(household=home(), name="Tofu bowls"))
        Feedback.objects.create(meal=past, kids="disliked", parents="loved", reaction="Leo: rash")
        request = MenuRequest.objects.create(household=home(), week=WEEK, slots=[{"date": "2026-10-05", "slot": "dinner", "eaters": []}])
        prompt = planner.build_prompt(request)
        self.assertIn("- Fish twice a week", prompt)
        self.assertNotIn("Old rule", prompt)
        self.assertIn("Max cooking time on weekdays: 30 min", prompt)
        self.assertIn("Tofu bowls - kids: didn't like it; parents: loved it; BAD REACTION: Leo: rash", prompt)

    def test_keeps_existing_meals_unless_replacing(self):
        kept = PlannedMeal.objects.create(household=home(), date="2026-10-05", dish=Dish.objects.create(household=home(), name="Pizza"))
        self._fake(reply(tool_call({"summary": "", "meals": [
            meal("2026-10-05", "dinner", "Tacos"), meal("2026-10-06", "dinner", "Soup"),
        ]})))
        self._post([("2026-10-05", "dinner", [self.mum]), ("2026-10-06", "dinner", [self.mum])])
        self.assertTrue(PlannedMeal.objects.filter(pk=kept.pk).exists())
        self.assertEqual(sorted(PlannedMeal.objects.values_list("dish__name", flat=True)), ["Pizza", "Soup"])

        MenuRequest.objects.all().delete()
        self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        self._post([("2026-10-05", "dinner", [self.mum])], replace=True)
        self.assertFalse(PlannedMeal.objects.filter(pk=kept.pk).exists())
        self.assertEqual(PlannedMeal.objects.get(date="2026-10-05").dish.name, "Tacos")

    def test_existing_dish_keeps_its_ingredients_and_details(self):
        dish = Dish.objects.create(household=home(), name="Salmon pasta", kind="fish", minutes=20, servings=4,
                                   recipe_url="https://example.com/mine")
        dish.ingredients.create(name="Salmon", quantity=600, unit="g", category="fish")
        self._fake(reply(tool_call({"summary": "", "meals": [
            meal("2026-10-05", "dinner", "salmon PASTA", [("Cod", 1, "kg", "fish")], servings=2,
                 url="https://other.example/x"),
        ]})))
        self._post([("2026-10-05", "dinner", [self.mum, self.kid])])
        dish.refresh_from_db()
        self.assertEqual((dish.recipe_url, dish.minutes), ("https://example.com/mine", 20))
        self.assertEqual(list(dish.ingredients.values_list("name", flat=True)), ["Salmon"])
        # Scaled to the planned portions.
        items = {i.name: i for s in shopping.build(home(), WEEK)[0] for i in s.items}
        self.assertEqual(items["Salmon"].quantity, "300 g")

    def test_meals_outside_the_request_are_ignored(self):
        self._fake(reply(tool_call({"summary": "", "meals": [
            meal("2026-10-05", "dinner", "Tacos"), meal("2026-10-05", "lunch", "Extra lunch"),
            meal("2026-10-20", "dinner", "Next month"),
        ]})))
        self._post([("2026-10-05", "dinner", [self.mum])])
        self.assertEqual(list(PlannedMeal.objects.values_list("dish__name", flat=True)), ["Tacos"])

    def test_reminds_to_save_when_answered_in_text(self):
        search = SimpleNamespace(type="web_search_call", status="completed")
        first = reply(search, message("Here is the plan: ..."))
        fake = self._fake(first, reply(tool_call({"summary": "ok", "meals": [meal("2026-10-05", "dinner", "Tofu")]})))
        self._post([("2026-10-05", "dinner", [self.mum])])
        request = MenuRequest.objects.get()
        self.assertEqual(request.status, "done")
        self.assertEqual((request.input_tokens, request.output_tokens), (2000, 1000))
        # The reminder continues the same conversation.
        self.assertEqual(fake.requests[1]["previous_response_id"], first.id)
        self.assertIn("calling save_menu", fake.requests[1]["input"][0]["content"])

    def test_gives_up_after_repeated_text_answers(self):
        self._fake(*[reply(message("Plan...")) for _ in range(planner.MAX_TURNS)])
        self._post([("2026-10-05", "dinner", [self.mum])])
        request = MenuRequest.objects.get()
        self.assertEqual(request.status, "failed")
        self.assertIn("didn't return a menu", request.error)

    def test_incomplete_response_fails_with_a_message(self):
        self._fake(reply(status="incomplete", reason="max_output_tokens"))
        self._post([("2026-10-05", "dinner", [self.mum])])
        self.assertIn("too long", MenuRequest.objects.get().error)

    def test_refusal_and_errors_fail_with_a_message(self):
        self._fake(reply(message("I can't help with that.", refusal=True)))
        self._post([("2026-10-05", "dinner", [self.mum])])
        request = MenuRequest.objects.get()
        self.assertEqual(request.status, "failed")
        self.assertIn("declined", request.error)
        page = self.client.get(reverse("meals:menu_request", args=[request.pk]))
        self.assertContains(page, "Try again")

        MenuRequest.objects.all().delete()
        self._fake(ConnectionError("network down"))
        with self.assertLogs("meals.planner", level="ERROR"):
            self._post([("2026-10-05", "dinner", [self.mum])])
        request = MenuRequest.objects.get()
        self.assertEqual(request.status, "failed")
        self.assertIn("ConnectionError", request.error)
        self.assertFalse(PlannedMeal.objects.exists())

    def test_unusable_answer_keeps_the_meals_it_would_replace(self):
        kept = PlannedMeal.objects.create(household=home(), date="2026-10-05", dish=Dish.objects.create(household=home(), name="Pizza"))
        self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-12-24", "dinner", "Wrong week")]})))
        self._post([("2026-10-05", "dinner", [self.mum])], replace=True)
        request = MenuRequest.objects.get()
        self.assertEqual(request.status, "failed")
        self.assertIn("didn't match", request.error)
        self.assertTrue(PlannedMeal.objects.filter(pk=kept.pk).exists())

    def test_request_expired_meanwhile_saves_nothing(self):
        fake = self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        create = fake.create

        def slow_create(**kwargs):
            # While waiting for the AI, the request is marked stale (e.g. the family started a new one).
            MenuRequest.objects.update(status="failed", error="This took too long and was stopped. Please try again.")
            return create(**kwargs)

        fake.responses.create = slow_create
        self._post([("2026-10-05", "dinner", [self.mum])])
        self.assertEqual(MenuRequest.objects.get().status, "failed")
        self.assertFalse(PlannedMeal.objects.exists())

    def test_time_budget(self):
        fake = self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        self._post([("2026-10-05", "dinner", [self.mum])])
        self.assertLessEqual(fake.timeouts[0], planner.TIME_BUDGET)
        MenuRequest.objects.all().delete()
        self._fake(reply(tool_call({"summary": "", "meals": []})))
        with mock.patch.object(planner, "TIME_BUDGET", 10):
            self._post([("2026-10-06", "dinner", [self.mum])])
        self.assertIn("took too long", MenuRequest.objects.get().error)

    def test_prompt_answer_and_cost_are_kept(self):
        search = SimpleNamespace(type="web_search_call", status="completed")
        self._fake(reply(search, tool_call({"summary": "ok", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        self._post([("2026-10-05", "dinner", [self.mum])])
        request = MenuRequest.objects.get()
        self.assertEqual(request.model, "gpt-6.1-sol")
        self.assertIn("## Meals to plan", request.prompt)
        self.assertEqual(json.loads(request.response)["meals"][0]["dish_name"], "Tacos")
        self.assertEqual(request.web_searches, 1)
        # 1000 input and 500 output tokens at $2 / $10 per million, plus one search at $0.01.
        self.assertEqual(request.cost, Decimal("0.0170"))
        self.assertIsNone(planner.cost("some-new-model", {"input": 1, "output": 1, "searches": 0}))

    def test_validation(self):
        response = self._post([])
        self.assertContains(response, "Choose at least one meal")
        response = self._post([("2026-10-05", "dinner", [])])
        self.assertContains(response, "choose who eats")
        self.assertFalse(MenuRequest.objects.exists())

    @override_settings(OPENAI_API_KEY="")
    def test_disabled_without_api_key(self):
        response = self.client.get(reverse("meals:menu_create", args=[WEEK.isoformat()]))
        self.assertContains(response, "set up yet (OPENAI_API_KEY is missing)")
        response = self._post([("2026-10-05", "dinner", [self.mum])])
        self.assertFalse(MenuRequest.objects.exists())

    def test_running_request_shows_progress_and_blocks_a_second(self):
        request = MenuRequest.objects.create(household=home(), week=WEEK, status="running", slots=[])
        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        self.assertContains(page, "Create menu in progress")
        response = self.client.get(reverse("meals:menu_create", args=[WEEK.isoformat()]))
        self.assertRedirects(response, reverse("meals:menu_request", args=[request.pk]))
        state = self.client.get(reverse("meals:menu_request_status", args=[request.pk])).json()
        self.assertEqual((state["status"], state["finished"]), ("running", False))

    def test_stale_requests_expire(self):
        request = MenuRequest.objects.create(household=home(), week=WEEK, status="running", slots=[])
        MenuRequest.objects.filter(pk=request.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        state = self.client.get(reverse("meals:menu_request_status", args=[request.pk])).json()
        self.assertEqual(state["status"], "failed")


class UsualWeekTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        self.a = FamilyMember.objects.create(household=home(), name="Ana")
        self.b = FamilyMember.objects.create(household=home(), name="Ben")

    def test_defaults(self):
        week = schedule.usual_week(home(), [self.a, self.b])
        self.assertEqual(week[0]["dinner"], {"on": True, "eaters": [self.a.pk, self.b.pk]})
        self.assertFalse(week[0]["lunch"]["on"])
        self.assertTrue(week[6]["lunch"]["on"])

    def test_edit_and_summary(self):
        data = {f"{d}-dinner-on": "on" for d in range(7)}
        data.update({f"{d}-dinner-eaters": [str(self.a.pk), str(self.b.pk)] for d in range(7)})
        data["2-dinner-eaters"] = [str(self.a.pk)]
        data["5-lunch-on"] = "on"
        data["5-lunch-eaters"] = [str(self.b.pk)]
        response = self.client.post(reverse("meals:usual_week"), data)
        self.assertRedirects(response, reverse("meals:family"))
        week = schedule.usual_week(home(), [self.a, self.b])
        self.assertEqual(week[2]["dinner"]["eaters"], [self.a.pk])
        self.assertTrue(week[5]["lunch"]["on"])
        self.assertFalse(week[6]["lunch"]["on"])
        page = self.client.get(reverse("meals:family"))
        self.assertContains(page, "Dinner – everyone")
        self.assertContains(page, "Dinner – Ana")
        self.assertContains(page, "Lunch – Ben · Dinner – everyone")

    def test_removed_members_are_dropped(self):
        week = schedule.usual_week(home(), [self.a, self.b])
        schedule.save_usual_week(home(), week)
        self.b.delete()
        self.assertEqual(schedule.usual_week(home(), [self.a])[0]["dinner"]["eaters"], [self.a.pk])


class ScalingTests(TestCase):
    def test_quantities_follow_planned_portions(self):
        dish = Dish.objects.create(household=home(), name="Chili", servings=4)
        dish.ingredients.create(name="Beans", quantity=2, unit="can", category="pantry")
        dish.ingredients.create(name="Mince", quantity=500, unit="g", category="meat")
        dish.ingredients.create(name="Cumin", quantity=Decimal("1"), unit="tsp", category="pantry")
        PlannedMeal.objects.create(household=home(), date=WEEK, dish=dish, servings=3)
        items = {i.name: i for s in shopping.build(home(), WEEK)[0] for i in s.items}
        self.assertEqual(items["Beans"].quantity, "2 can")  # 1.5 rounded up
        self.assertEqual(items["Mince"].quantity, "375 g")
        self.assertEqual(items["Cumin"].quantity, "0.8 tsp")



@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini", AI_EFFORT="medium", TIME_ZONE="Europe/Berlin")
class ChangeMenuTests(TestCase):
    """Change menu, replacing one dish, and the week page around them."""

    def setUp(self):
        self.client.force_login(User.objects.create_user(email="parent@example.com"))
        approve_ai()
        self.ana = FamilyMember.objects.create(household=home(), name="Ana")
        self.leo = FamilyMember.objects.create(household=home(), name="Leo", kind="child")
        patcher = mock.patch.object(planner, "start", side_effect=lambda request: planner.run(request.pk))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(planner, "link_works", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.today = date(2026, 10, 4)  # a Sunday, so the week of 5 Oct is in the future
        patcher = mock.patch("django.utils.timezone.localdate", return_value=self.today)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake(self, *replies):
        fake = FakeOpenAI(*replies)
        patcher = mock.patch.object(planner, "get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _created_week(self):
        """A week created earlier, with its 'About this menu'."""
        MenuRequest.objects.create(household=home(), week=WEEK, kind="create", status="done", summary="Original summary.", slots=[])
        stew = Dish.objects.create(household=home(), name="Stew")
        tuesday = PlannedMeal.objects.create(household=home(), date="2026-10-06", dish=Dish.objects.create(household=home(), name="Tacos"))
        tuesday.eaters.set([self.ana])
        saturday = PlannedMeal.objects.create(household=home(), date="2026-10-10", dish=stew, servings=8)
        sunday = PlannedMeal.objects.create(household=home(), date="2026-10-11", slot="lunch", dish=stew, leftovers=True)
        return tuesday, saturday, sunday

    def test_week_page_layout(self):
        self._created_week()
        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        content = page.content.decode()
        self.assertNotContains(page, "Plan with AI")
        self.assertNotContains(page, "✨ Create menu")
        # Change menu after the meal count, then the folded "About this menu".
        self.assertLess(content.index("meals planned this week"), content.index("✨ Change menu"))
        self.assertLess(content.index("✨ Change menu"), content.index("About this menu"))
        self.assertIn('<details class="card about-menu">', content)
        self.assertIn("Original summary.", content)

    def test_empty_week_shows_create_menu_at_the_top(self):
        page = self.client.get(reverse("meals:menu_of", args=["2026-10-12"]))
        self.assertContains(page, "✨ Create menu")
        self.assertNotContains(page, "Change menu")

    def test_change_menu_ticks_all_meals_and_replaces_by_default(self):
        self._created_week()
        response = self.client.get(reverse("meals:menu_create", args=[WEEK.isoformat()]))
        self.assertTrue(response.context["changing"])
        self.assertFalse(response.context["keep_existing"])
        self.assertContains(response, '<input type="checkbox" name="replace" checked>')
        rows = {r["key"]: {s["slot"]: s for s in r["slots"]} for r in response.context["rows"]}
        tuesday = rows["2026-10-06"]["dinner"]
        self.assertTrue(tuesday["on"])
        self.assertEqual([e["eats"] for e in tuesday["members"]], [True, False])  # planned for Ana only
        self.assertTrue(rows["2026-10-11"]["lunch"]["on"])

    def test_past_days_start_unticked(self):
        self._created_week()
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 10, 8)):  # Thursday
            response = self.client.get(reverse("meals:menu_create", args=[WEEK.isoformat()]))
        rows = {r["key"]: {s["slot"]: s["on"] for s in r["slots"]} for r in response.context["rows"]}
        self.assertEqual(rows["2026-10-06"], {"lunch": False, "dinner": False})  # Tuesday's tacos were eaten
        self.assertTrue(rows["2026-10-10"]["dinner"])
        self.assertTrue(rows["2026-10-11"]["lunch"])

    def test_change_menu_rewrites_about_this_menu(self):
        self._created_week()
        self._fake(reply(tool_call({"summary": "A new summary.", "meals": [meal("2026-10-06", "dinner", "Soup")]})))
        self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {
            "2026-10-06-dinner-on": "on", "2026-10-06-dinner-eaters": [str(self.ana.pk)], "replace": "on",
        })
        self.assertEqual(MenuRequest.objects.first().kind, "change")
        self.assertEqual(PlannedMeal.objects.get(date="2026-10-06").dish.name, "Soup")
        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        self.assertContains(page, "A new summary.")
        self.assertNotContains(page, "Original summary.")

    def test_replace_button_only_from_today(self):
        tuesday, *_ = self._created_week()
        past = PlannedMeal.objects.create(household=home(), date="2026-10-02", dish=Dish.objects.create(household=home(), name="Old"))
        self.assertContains(self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()])),
                            reverse("meals:meal_replace", args=[tuesday.pk]))
        self.assertNotContains(self.client.get(reverse("meals:menu_of", args=["2026-09-28"])),
                               reverse("meals:meal_replace", args=[past.pk]))

    def test_replace_dish_with_reason(self):
        tuesday, *_ = self._created_week()
        response = self.client.get(reverse("meals:meal_replace", args=[tuesday.pk]))
        self.assertContains(response, "Why do you want to replace it?")
        fake = self._fake(reply(tool_call({"summary": "x", "meals": [meal("2026-10-06", "dinner", "Fish pie")]})))
        self.client.post(reverse("meals:meal_replace", args=[tuesday.pk]), {"reason": "We had tacos on Friday"})
        request = MenuRequest.objects.first()
        self.assertEqual((request.kind, request.replacing, request.keep_existing), ("replace", "Tacos", False))
        self.assertEqual(request.slots, [{"date": "2026-10-06", "slot": "dinner", "eaters": [self.ana.pk]}])
        self.assertEqual(PlannedMeal.objects.get(date="2026-10-06").dish.name, "Fish pie")
        self.assertEqual(PlannedMeal.objects.count(), 3)  # the rest of the week is untouched
        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        self.assertContains(page, "Original summary.")  # replacing one dish keeps "About this menu"
        self.assertNotContains(page, "<p>x</p>")
        prompt = fake.requests[0]["input"][0]["content"]
        self.assertIn('instead of "Tacos"', prompt)
        self.assertIn("Their reason: We had tacos on Friday", prompt)
        self.assertIn("Sat 10 Oct dinner: Stew", prompt)  # the current menu is sent as context
        self.assertNotIn("## Notes for this week", prompt)
        page = self.client.get(reverse("meals:menu_of", args=[WEEK.isoformat()]))
        self.assertContains(page, "Original summary.")

    def test_replacing_a_dish_includes_its_leftovers(self):
        _, saturday, sunday = self._created_week()
        self._fake(reply(tool_call({"summary": "x", "meals": [
            meal("2026-10-10", "dinner", "Lasagne", servings=8), meal("2026-10-11", "lunch", "Lasagne", leftovers=True),
        ]})))
        self.client.post(reverse("meals:meal_replace", args=[saturday.pk]), {"reason": ""})
        request = MenuRequest.objects.first()
        self.assertEqual([(s["date"], s["slot"]) for s in request.slots], [("2026-10-10", "dinner"), ("2026-10-11", "lunch")])
        self.assertEqual(
            sorted(PlannedMeal.objects.filter(date__gte="2026-10-10").values_list("dish__name", "leftovers")),
            [("Lasagne", False), ("Lasagne", True)],
        )
        self.assertEqual(Dish.objects.get(name="Stew").planned.count(), 0)


@override_settings(OPENAI_API_KEY="test-key", TIME_ZONE="Europe/Berlin")
class RecipeSitesTests(TestCase):
    def setUp(self):
        self.request = MenuRequest.objects.create(household=home(), week=WEEK, slots=[{"date": "2026-10-05", "slot": "dinner", "eaters": []}])

    def _household(self, sites, other):
        household = self.request.household
        household.recipe_sites, household.other_sites = sites, other
        household.save()
        return household

    def test_websites_and_names_are_parsed(self):
        self.assertEqual(
            planner.recipe_sources("https://www.bbcgoodfood.com/recipes\nchefkoch.de, jaime  oliver\n\nOttolenghi; BBC.co.uk"),
            (["bbcgoodfood.com", "chefkoch.de", "bbc.co.uk"], ["jaime oliver", "Ottolenghi"]),
        )

    def test_names_go_into_the_prompt_and_lift_the_search_limit(self):
        household = self._household("bbcgoodfood.com\njaime oliver", "never")
        prompt = planner.build_prompt(self.request)
        self.assertIn("Recipe sources to search first (websites, cooks or brands): bbcgoodfood.com, jaime oliver", prompt)
        self.assertIn("Recipes from other sources: Never – only the sources above", prompt)
        # Jamie Oliver's site isn't known, so the search can't be limited to the listed sites.
        self.assertNotIn("filters", planner.web_search_tool(household))

    def test_only_names(self):
        self._household("Jamie Oliver", "rarely")
        self.assertIn("search first (websites, cooks or brands): Jamie Oliver", planner.build_prompt(self.request))

    def test_sites_and_frequency_go_into_the_prompt(self):
        self._household("bbcgoodfood.com\nchefkoch.de", "rarely")
        prompt = planner.build_prompt(self.request)
        self.assertIn("Recipe sources to search first (websites, cooks or brands): bbcgoodfood.com, chefkoch.de", prompt)
        self.assertIn("Recipes from other sources: Rarely – about one recipe a week", prompt)
        self.assertNotIn("filters", planner.web_search_tool(home()))

    def test_never_limits_the_search_to_those_sites(self):
        household = self._household("bbcgoodfood.com\nchefkoch.de", "never")
        self.assertEqual(planner.web_search_tool(household)["filters"], {"allowed_domains": ["bbcgoodfood.com", "chefkoch.de"]})

    def test_no_sites_no_mention(self):
        household = self._household("", "never")
        self.assertNotIn("Recipe sources", planner.build_prompt(self.request))
        self.assertNotIn("filters", planner.web_search_tool(household))



class RecipeLinkTests(TestCase):
    def test_tollbit_hosts_become_the_normal_site(self):
        self.assertEqual(planner.clean_url("https://tollbit.bbcgoodfood.com/recipes/stew"),
                         "https://www.bbcgoodfood.com/recipes/stew")
        self.assertEqual(planner.clean_url(" https://www.chefkoch.de/x "), "https://www.chefkoch.de/x")

    def test_link_check_only_drops_clearly_missing_pages(self):
        from urllib.error import HTTPError, URLError

        def check(error):
            with mock.patch.object(planner, "urlopen", side_effect=error):
                return planner.link_works("https://example.com/r")

        self.assertFalse(check(HTTPError("u", 404, "Not Found", {}, None)))
        self.assertFalse(check(HTTPError("u", 410, "Gone", {}, None)))
        self.assertFalse(check(URLError(OSError("Name or service not known"))))
        self.assertTrue(check(HTTPError("u", 403, "Forbidden", {}, None)))  # bot protection
        self.assertTrue(check(HTTPError("u", 500, "Error", {}, None)))
        self.assertTrue(check(URLError(TimeoutError())))
        self.assertTrue(check(TimeoutError()))

    def test_clean_links(self):
        data = {"meals": [
            {"recipe_url": "https://tollbit.bbcgoodfood.com/a"},
            {"recipe_url": "https://gone.example/b"},
            {"recipe_url": ""},
        ]}
        with mock.patch.object(planner, "link_works", side_effect=lambda url: "gone" not in url) as works:
            planner.clean_links(data)
        self.assertEqual([m["recipe_url"] for m in data["meals"]], ["https://www.bbcgoodfood.com/a", "", ""])
        self.assertEqual(works.call_count, 2)


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini", CSRF_TRUSTED_ORIGINS=["https://chef.example.com"], SITE_URL="")
class MenuEmailTests(TestCase):
    def setUp(self):
        self.pat = User.objects.create_user(email="pat@example.com", name="Pat Parent")
        self.sam = User.objects.create_user(email="sam@example.com", name="Sam", household=self.pat.household)
        approve_ai()
        self.client.force_login(self.pat)
        for name, target in [("start", lambda request: planner.run(request.pk)), ("link_works", None)]:
            patcher = mock.patch.object(planner, name, side_effect=target) if target else mock.patch.object(planner, name, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake(self, *replies):
        patcher = mock.patch.object(planner, "get_client", return_value=FakeOpenAI(*replies))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self):
        return self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {"2026-10-05-dinner-on": "on"})

    def test_the_household_gets_the_new_menu(self):
        self._fake(reply(tool_call({"summary": "A calm week.", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        self._create()
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["pat@example.com", "sam@example.com"])  # the one who asked too
        self.assertEqual(email.subject, "Chef: New menu for the week of 5 Oct")
        self.assertIn("Pat created the menu for the week of 5 October", email.body)
        self.assertIn("- Mon 5 Oct, dinner: Tacos", email.body)
        self.assertIn("A calm week.", email.body)
        self.assertIn("https://chef.example.com/week/2026-10-05/", email.body)

    def test_replaced_dish(self):
        tacos = PlannedMeal.objects.create(household=home(), date="2026-10-05", dish=Dish.objects.create(household=home(), name="Tacos"))
        summary = "Summary of the replacement"
        self._fake(reply(tool_call({"summary": summary, "meals": [meal("2026-10-05", "dinner", "Fish pie")]})))
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 10, 4)):
            self.client.post(reverse("meals:meal_replace", args=[tacos.pk]), {"reason": "Too spicy"})
        self.assertEqual(mail.outbox[0].subject, "Chef: Tacos was replaced on the menu")
        self.assertIn('Pat replaced Tacos on the menu ("Too spicy")', mail.outbox[0].body)
        self.assertIn("Fish pie", mail.outbox[0].body)
        self.assertNotIn(summary, mail.outbox[0].body)  # only new menus carry a summary

    def test_no_email_when_it_failed_or_is_switched_off(self):
        self._fake(reply(message("no", refusal=True)))
        self._create()
        self.assertEqual(mail.outbox, [])
        MenuRequest.objects.all().delete()
        self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        with override_settings(MENU_EMAILS=False):
            self._create()
        self.assertEqual(MenuRequest.objects.get().status, "done")
        self.assertEqual(mail.outbox, [])

    def test_mail_failure_does_not_break_the_menu(self):
        self._fake(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Tacos")]})))
        with mock.patch("meals.notify.send_mail", side_effect=OSError("smtp down")), self.assertLogs("meals.notify", "ERROR"):
            self._create()
        self.assertEqual(MenuRequest.objects.get().status, "done")
