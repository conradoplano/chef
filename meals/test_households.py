"""Several households: their data kept apart, the people in them, AI costs and limits, the admin page."""
import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import budget, planner, recipe_import
from .models import (
    AIUsage, Dish, ExtraItem, FamilyMember, Household, MenuRequest, PlannedMeal, RecipeImport, Rule, ShoppingCheck,
    WeeklyItem,
)
from .test_planner import FakeOpenAI, meal, reply, tool_call

WEEK = date(2026, 10, 5)


class IsolationTests(TestCase):
    """Nobody sees or changes another household's data, even with its addresses."""

    def setUp(self):
        self.ours = User.objects.create_user(email="pat@example.com", name="Pat")
        self.theirs = User.objects.create_user(email="sam@example.com", name="Sam")
        self.other = self.theirs.household
        self.dish = Dish.objects.create(household=self.other, name="Secret stew", recipe_url="https://example.com/stew")
        self.dish.ingredients.create(name="Saffron", quantity=1, unit="g", category="pantry")
        self.meal = PlannedMeal.objects.create(household=self.other, date=WEEK, dish=self.dish)
        self.member = FamilyMember.objects.create(household=self.other, name="Ximena", allergies="Sesame")
        self.rule = Rule.objects.create(household=self.other, text="No fish on Fridays")
        self.extra = ExtraItem.objects.create(household=self.other, week=WEEK, name="Candles")
        WeeklyItem.objects.create(household=self.other, name="Pumpernickel")
        self.client.force_login(self.ours)

    def test_pages_show_only_our_household(self):
        for url in [reverse("meals:menu_of", args=[WEEK.isoformat()]), reverse("meals:shopping_of", args=[WEEK.isoformat()]),
                    reverse("meals:recipes") + "?show=all", reverse("meals:family"), reverse("meals:home"),
                    reverse("meals:meal_pick") + f"?date={WEEK.isoformat()}"]:
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            for secret in ["Secret stew", "Saffron", "Candles", "Pumpernickel", "Ximena", "No fish on Fridays", "sam@example.com"]:
                self.assertNotContains(page, secret, msg_prefix=url)

    def test_their_things_are_not_found(self):
        gets = [
            reverse("meals:recipe", args=[self.dish.pk]), reverse("meals:recipe_edit", args=[self.dish.pk]),
            reverse("meals:ingredients", args=[self.dish.pk]), reverse("meals:meal", args=[self.meal.pk]),
            reverse("meals:feedback", args=[self.meal.pk]), reverse("meals:meal_replace", args=[self.meal.pk]),
            reverse("meals:member", args=[self.member.pk]), reverse("meals:rule", args=[self.rule.pk]),
        ]
        for url in gets:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        posts = [
            (reverse("meals:feedback_quick", args=[self.meal.pk]), {"rating": "loved"}),
            (reverse("meals:recipe_status", args=[self.dish.pk]), {"status": "favourite"}),
            (reverse("meals:rule_toggle", args=[self.rule.pk]), {}),
            (reverse("meals:extra_delete", args=[self.extra.pk]), {}),
            (reverse("meals:member", args=[self.member.pk]), {"delete": "1"}),
            (reverse("meals:person_remove", args=[self.theirs.pk]), {}),
            (reverse("meals:meal_pick"), {"date": "2026-10-06", "slot": "dinner", "dish": self.dish.pk}),
        ]
        for url, data in posts:
            self.assertEqual(self.client.post(url, data).status_code, 404, url)
        self.assertTrue(self.rule.__class__.objects.get(pk=self.rule.pk).active)
        self.assertTrue(User.objects.filter(pk=self.theirs.pk).exists())
        self.assertFalse(PlannedMeal.objects.filter(household=self.ours.household).exists())

    def test_same_dish_name_in_both_households(self):
        page = self.client.post(reverse("meals:recipe_new"), {"name": "Secret stew", "kind": "meat", "servings": "4", "status": "try"})
        self.assertEqual(page.status_code, 302)
        self.assertEqual(Dish.objects.filter(name="Secret stew").count(), 2)
        self.assertEqual(Dish.objects.get(name="Secret stew", household=self.ours.household).status, "try")

    def test_known_link_is_only_ours(self):
        """A link the other household saved isn't "already in your binder"."""
        with mock.patch.object(recipe_import, "start"), override_settings(OPENAI_API_KEY="test-key"):
            Household.objects.update(ai_approved=True)
            response = self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/stew"})
        job = RecipeImport.objects.get()
        self.assertEqual(job.household, self.ours.household)
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]), fetch_redirect_response=False)

    def test_ticks_are_per_household(self):
        self.client.post(reverse("meals:shopping_toggle", args=[WEEK.isoformat()]), {"key": "saffron", "checked": "1"})
        self.assertEqual(ShoppingCheck.objects.get().household, self.ours.household)
        ShoppingCheck.objects.create(household=self.other, week=WEEK, key="saffron")  # no clash
        state = self.client.get(reverse("meals:shopping_state", args=[WEEK.isoformat()])).json()
        self.assertEqual(state["checked"], {})  # not on our list

    def test_prompt_has_only_our_family(self):
        FamilyMember.objects.create(household=self.ours.household, name="Ana")
        request = MenuRequest.objects.create(household=self.ours.household, week=WEEK, slots=[])
        prompt = planner.build_prompt(request)
        self.assertIn("Ana", prompt)
        self.assertNotIn("Ximena", prompt)
        self.assertNotIn("Secret stew", prompt)


class HouseholdBasicsTests(TestCase):
    def test_new_user_gets_a_household_of_their_own(self):
        a = User.objects.create_user(email="a@example.com")
        b = User.objects.create_user(email="b@example.com")
        self.assertNotEqual(a.household, b.household)
        self.assertFalse(a.household.ai_approved)

    def test_user_without_household_gets_one_on_their_next_visit(self):
        user = User.objects.create_user(email="a@example.com")
        User.objects.filter(pk=user.pk).update(household=None)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("meals:home")).status_code, 200)
        user.refresh_from_db()
        self.assertIsNotNone(user.household)

    def test_suspended_household_is_logged_out(self):
        user = User.objects.create_user(email="a@example.com")
        Household.objects.update(is_active=False)
        self.client.force_login(user)
        response = self.client.get(reverse("meals:home"), follow=True)
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertContains(response, "suspended")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_adduser_join_and_admin(self):
        call_command("adduser", "pat@example.com", "--admin", stdout=StringIO())
        call_command("adduser", "sam@example.com", "--join", "pat@example.com", stdout=StringIO())
        pat, sam = User.objects.get(email="pat@example.com"), User.objects.get(email="sam@example.com")
        self.assertEqual(pat.household, sam.household)
        self.assertTrue(pat.is_staff and pat.household.ai_approved)


class PeopleTests(TestCase):
    def setUp(self):
        self.pat = User.objects.create_user(email="pat@example.com", name="Pat Parent")
        self.household = self.pat.household
        self.client.force_login(self.pat)

    def _invite(self, email, name=""):
        return self.client.post(reverse("meals:person_invite"), {"invite-email": email, "invite-name": name}, follow=True)

    def test_add_someone_to_the_household(self):
        response = self._invite("Sam@Example.com", "Sam")
        self.assertContains(response, "Added Sam.")
        sam = User.objects.get(email="sam@example.com")
        self.assertEqual(sam.household, self.household)
        self.assertEqual(mail.outbox[0].to, ["sam@example.com"])
        self.assertIn("Pat added you", mail.outbox[0].body)
        self.assertContains(response, "sam@example.com")

    def test_one_household_per_email(self):
        User.objects.create_user(email="sam@example.com")
        response = self._invite("sam@example.com")
        self.assertContains(response, "already uses")
        self.assertNotEqual(User.objects.get(email="sam@example.com").household, self.household)
        self.assertContains(self._invite("pat@example.com"), "already in your household")

    def test_remove_someone(self):
        self._invite("sam@example.com")
        sam = User.objects.get(email="sam@example.com")
        self.client.post(reverse("meals:person_remove", args=[sam.pk]))
        self.assertFalse(User.objects.filter(pk=sam.pk).exists())

    def test_last_person_cant_be_removed(self):
        response = self.client.post(reverse("meals:person_remove", args=[self.pat.pk]), follow=True)
        self.assertContains(response, "only one in this household")
        self.assertTrue(User.objects.filter(pk=self.pat.pk).exists())
        self.assertNotContains(response, ">Leave</button>")

    def test_leave(self):
        self._invite("sam@example.com")
        response = self.client.post(reverse("meals:person_remove", args=[self.pat.pk]))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertFalse(User.objects.filter(pk=self.pat.pk).exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admins_keep_their_account(self):
        admin = User.objects.create_user(email="admin@example.com", household=self.household, is_staff=True)
        self.client.post(reverse("meals:person_remove", args=[admin.pk]))
        admin.refresh_from_db()
        self.assertNotEqual(admin.household, self.household)

    def test_rename(self):
        self.client.post(reverse("meals:household_rename"), {"household-name": "The Parents"})
        self.household.refresh_from_db()
        self.assertEqual(self.household.name, "The Parents")
        self.assertContains(self.client.get(reverse("meals:family")), 'value="The Parents"')

    @override_settings(MENU_EMAILS=True, OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini")
    def test_menu_email_only_to_our_household(self):
        User.objects.create_user(email="sam@example.com", household=self.household)
        User.objects.create_user(email="stranger@example.com")
        Household.objects.update(ai_approved=True)
        with mock.patch.object(planner, "start", side_effect=lambda r: planner.run(r.pk)), \
                mock.patch.object(planner, "link_works", return_value=True), \
                mock.patch.object(planner, "get_client", return_value=FakeOpenAI(reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Soup")]})))):
            self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {"2026-10-05-dinner-on": "on"})
        self.assertEqual(MenuRequest.objects.get().status, "done")
        self.assertEqual([m.to for m in mail.outbox], [["pat@example.com", "sam@example.com"]])


@override_settings(OPENAI_API_KEY="test-key")
class RegistrationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", is_staff=True)

    def _register(self, **data):
        fields = {"name": "Kim Lee", "email": "kim@example.com", "household": "The Lees", **data}
        return self.client.post(reverse("accounts:register"), fields)

    def _code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_register_with_a_code(self):
        self.assertContains(self.client.get(reverse("accounts:login")), 'href="/accounts/register/"')
        self.assertRedirects(self._register(), reverse("accounts:verify"))
        self.assertFalse(User.objects.filter(email="kim@example.com").exists())  # not before the code
        self.assertEqual(mail.outbox[0].to, ["kim@example.com"])
        self.assertContains(self.client.get(reverse("accounts:verify")), "Create account")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("accounts:verify"), {"code": self._code()}, follow=True)
        self.assertRedirects(response, reverse("meals:family"))
        self.assertContains(response, "Welcome!")
        self.assertContains(response, "switched on once an admin has approved")
        kim = User.objects.get(email="kim@example.com")
        self.assertEqual((kim.name, kim.household.name, kim.household.ai_approved), ("Kim Lee", "The Lees", False))
        self.assertEqual(mail.outbox[-1].to, ["admin@example.com"])
        self.assertIn("Kim Lee <kim@example.com> registered the household \"The Lees\"", mail.outbox[-1].body)

    def test_wrong_code_creates_nothing(self):
        self._register()
        code = self._code()
        self.client.post(reverse("accounts:verify"), {"code": f"{(int(code) + 1) % 10**6:06d}"})
        self.assertFalse(User.objects.filter(email="kim@example.com").exists())

    def test_known_email_just_logs_in(self):
        kim = User.objects.create_user(email="kim@example.com")
        self._register(email="KIM@example.com")
        self.client.post(reverse("accounts:verify"), {"code": self._code()})
        self.assertEqual(int(self.client.session["_auth_user_id"]), kim.pk)
        self.assertEqual(User.objects.filter(email="kim@example.com").count(), 1)

    def test_household_name_is_optional(self):
        self._register(household="")
        self.client.post(reverse("accounts:verify"), {"code": self._code()})
        self.assertEqual(User.objects.get(email="kim@example.com").household.name, "Kim's household")

    @override_settings(REGISTRATION_OPEN=False)
    def test_closed(self):
        self.assertEqual(self.client.get(reverse("accounts:register")).status_code, 404)
        self.assertNotContains(self.client.get(reverse("accounts:login")), "Create an account")


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini", AI_DAILY_LIMIT_USD=Decimal("0.20"),
                   AI_GLOBAL_DAILY_LIMIT_USD=Decimal("2.00"))
class BudgetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com")
        self.household = self.user.household
        self.household.ai_approved = True
        self.household.save()
        self.client.force_login(self.user)

    def _spend(self, cost, household=None, when=None):
        return AIUsage.objects.create(household=household or self.household, kind="menu", cost=Decimal(cost),
                                      **({"created_at": when} if when else {}))

    def test_waits_for_approval(self):
        self.household.ai_approved = False
        self.household.save()
        self.assertIn("once an admin has approved", budget.blocked(self.household))
        page = self.client.get(reverse("meals:menu_create", args=[WEEK.isoformat()]))
        self.assertContains(page, "once an admin has approved")
        self.assertContains(page, "disabled>")

    def test_household_limit(self):
        self.assertEqual(budget.blocked(self.household), "")
        self._spend("0.15", when=timezone.now() - timedelta(days=1))  # yesterday doesn't count
        self._spend("0.15")
        self.assertEqual(budget.blocked(self.household), "")
        self._spend("0.05")
        self.assertIn("used today's AI budget ($0.20)", budget.blocked(self.household))
        response = self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {"2026-10-05-dinner-on": "on"})
        self.assertContains(response, "used today&#x27;s AI budget")
        self.assertFalse(MenuRequest.objects.exists())

    def test_own_limit_and_switched_off(self):
        self._spend("0.25")
        self.household.ai_daily_limit = Decimal("1.00")
        self.assertEqual(budget.blocked(self.household), "")
        self.household.ai_daily_limit = Decimal("0")
        self.assertIn("switched off", budget.blocked(self.household))

    def test_everyones_limit(self):
        other = User.objects.create_user(email="sam@example.com").household
        other.ai_daily_limit = Decimal("5")
        self._spend("2.00", household=other)
        self.assertIn("The app has used its AI budget", budget.blocked(self.household))

    def test_every_call_is_written_to_the_ledger(self):
        fake = FakeOpenAI(reply(), reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Soup")]})))
        with mock.patch.object(planner, "start", side_effect=lambda r: planner.run(r.pk)), \
                mock.patch.object(planner, "link_works", return_value=True), \
                mock.patch.object(planner, "get_client", return_value=fake):
            self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {"2026-10-05-dinner-on": "on"})
        request = MenuRequest.objects.get()
        usage = list(AIUsage.objects.all())
        self.assertEqual(len(usage), 2)  # a text answer, then the menu
        self.assertEqual({(u.household, u.user, u.kind, u.model) for u in usage}, {(self.household, self.user, "menu", "gpt-5.4-mini")})
        self.assertEqual(sum(u.cost for u in usage), request.cost)

    def test_failed_and_discarded_imports_still_count(self):
        fake = FakeOpenAI(reply(), reply())  # never answers with a recipe
        with mock.patch.object(recipe_import, "start", side_effect=lambda job: recipe_import.run(job.pk)), \
                mock.patch.object(recipe_import, "fetch_page", return_value=""), \
                mock.patch.object(planner, "get_client", return_value=fake):
            self.client.post(reverse("meals:recipe_link_new"), {"url": "https://example.com/soup"})
        job = RecipeImport.objects.get()
        self.assertEqual(job.status, "failed")
        self.client.post(reverse("meals:recipe_import_discard", args=[job.pk]))
        self.assertFalse(RecipeImport.objects.exists())
        self.assertEqual(AIUsage.objects.filter(household=self.household, kind="recipe").count(), 2)

    def test_prices_for_dated_model_versions(self):
        self.assertEqual(planner.prices_for("gpt-5.4-mini-2026-03-17"), planner.PRICES["gpt-5.4-mini"])
        self.assertEqual(planner.prices_for(" GPT-6.1-sol "), planner.PRICES["gpt-6.1-sol"])
        self.assertEqual(planner.prices_for("gpt-5.4"), (Decimal("2.50"), Decimal("15.00")))
        self.assertEqual(planner.prices_for("gpt-5.4-2026-03-05"), planner.PRICES["gpt-5.4"])
        # A longer known name wins over gpt-5.4 itself.
        self.assertEqual(planner.prices_for("gpt-5.4-nano-2026-03-17"), planner.PRICES["gpt-5.4-nano"])
        self.assertIsNone(planner.prices_for("gpt-5"))
        usage = {"input": 1_000_000, "output": 0, "searches": 1}
        self.assertEqual(planner.cost("gpt-5.4-mini-2026-03-17", usage), Decimal("0.7600"))

    def test_ledger_has_the_model_that_answered(self):
        answer = reply(tool_call({"summary": "", "meals": [meal("2026-10-05", "dinner", "Soup")]}))
        answer.model = "gpt-5.4-mini-2026-03-17"
        with mock.patch.object(planner, "start", side_effect=lambda r: planner.run(r.pk)),                 mock.patch.object(planner, "link_works", return_value=True),                 mock.patch.object(planner, "get_client", return_value=FakeOpenAI(answer)):
            self.client.post(reverse("meals:menu_create", args=[WEEK.isoformat()]), {"2026-10-05-dinner-on": "on"})
        entry = AIUsage.objects.get()
        self.assertEqual(entry.model, "gpt-5.4-mini-2026-03-17")
        self.assertGreater(entry.cost, 0)

    def test_usage_on_the_settings_page(self):
        self._spend("0.12")
        page = self.client.get(reverse("meals:family"))
        self.assertContains(page, "$0.12 of $0.20")


class AdminPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", is_staff=True)
        self.kim = User.objects.create_user(email="kim@example.com", name="Kim")
        self.lees = self.kim.household
        self.lees.name = "The Lees"
        self.lees.save()
        AIUsage.objects.create(household=self.lees, kind="recipe", model="gpt-5.4-mini", cost=Decimal("0.0300"))
        self.client.force_login(self.admin)

    def _act(self, household, **data):
        return self.client.post(reverse("meals:manage_household", args=[household.pk]), data, follow=True)

    def test_only_for_admins(self):
        self.client.force_login(self.kim)
        self.assertEqual(self.client.get(reverse("meals:manage")).status_code, 403)
        self.assertEqual(self._act(self.lees, action="approve").status_code, 403)
        self.lees.refresh_from_db()
        self.assertFalse(self.lees.ai_approved)

    def test_overview(self):
        page = self.client.get(reverse("meals:manage"))
        self.assertEqual([h.pk for h in page.context["pending"]], [self.lees.pk, self.admin.household.pk])
        self.assertContains(page, "kim@example.com")
        self.assertContains(page, "Today $0.03 of $0.20")
        self.assertContains(page, "<td>gpt-5.4-mini</td><td>1</td>")

    def test_approve_limit_suspend(self):
        self._act(self.lees, action="approve")
        self._act(self.lees, action="limit", limit="0.5")
        self.lees.refresh_from_db()
        self.assertEqual((self.lees.ai_approved, self.lees.ai_daily_limit), (True, Decimal("0.50")))
        self.assertContains(self._act(self.lees, action="limit", limit="lots"), "Enter the limit in dollars")
        self._act(self.lees, action="limit", limit="")
        self._act(self.lees, action="suspend")
        self.lees.refresh_from_db()
        self.assertEqual((self.lees.ai_daily_limit, self.lees.is_active), (None, False))
        self._act(self.lees, action="reactivate")
        self.lees.refresh_from_db()
        self.assertTrue(self.lees.is_active)

    def test_calls_without_a_price_are_flagged(self):
        AIUsage.objects.create(household=self.lees, kind="menu", model="gpt-9", input_tokens=5000, cost=0)
        self.assertContains(self.client.get(reverse("meals:manage")), "1 call with the model “gpt-9” has no price, so it counts as $0")

    def test_not_your_own_household(self):
        self.assertContains(self._act(self.admin.household, action="suspend"), "your own household")
        self.admin.household.refresh_from_db()
        self.assertTrue(self.admin.household.is_active)


class PriceMissingCostsMigrationTests(TestCase):
    """0022 fills in costs recorded as missing because the model had no price yet."""

    def test_fills_in_known_models_only(self):
        from importlib import import_module

        from django.apps import apps

        household = User.objects.create_user(email="pat@example.com").household
        tokens = {"input_tokens": 1_000_000, "output_tokens": 100_000, "web_searches": 2}
        missing = AIUsage.objects.create(household=household, kind="menu", model="gpt-5.4-2026-03-05", cost=0, **tokens)
        unknown = AIUsage.objects.create(household=household, kind="menu", model="gpt-9", cost=0, **tokens)
        priced = AIUsage.objects.create(household=household, kind="recipe", model="gpt-5.4", cost=Decimal("0.0100"), **tokens)
        request = MenuRequest.objects.create(household=household, week=WEEK, slots=[], model="gpt-5.4", **tokens)

        import_module("meals.migrations.0022_price_missing_ai_costs").price_missing_costs(apps, None)

        # 1M input at $2.50 + 100k output at $15.00 + 2 searches at $0.01
        for row in (missing, request):
            row.refresh_from_db()
            self.assertEqual(row.cost, Decimal("4.0200"))
        unknown.refresh_from_db()
        priced.refresh_from_db()
        self.assertEqual((unknown.cost, priced.cost), (Decimal("0"), Decimal("0.0100")))
