"""A recipe written by AI from an idea, with or without a few questions first."""
import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from . import planner, recipe_import
from .models import AIUsage, Dish, FamilyMember, PlannedMeal, RecipeImport
from .test_photos import recipe_call
from .test_planner import FakeOpenAI, reply
from .testing import approve_ai, freeze_now

IDEA = "pork gyros in pita bread, cucumbers and small tomatoes, with ready-made gyros meat from the counter"
GYROS = [
    {"name": "Gyros meat", "quantity": 600, "unit": "g", "category": "meat", "note": "ready-made, from the counter", "check": False},
    {"name": "Pita breads", "quantity": 4, "unit": "pcs", "category": "bakery", "note": "", "check": False},
    {"name": "Cucumber", "quantity": 1, "unit": "pcs", "category": "vegetables", "note": "", "check": False},
]


def questions_call(*questions):
    return SimpleNamespace(type="function_call", name="ask_questions", arguments=json.dumps({"questions": list(questions)}), call_id="q1")


def gyros_call():
    return recipe_call(name="Pork gyros pitas", kind="meat", minutes=20, servings=3, source="Somewhere",
                       online_url="https://example.com/should-not-be-used", ingredients=GYROS,
                       instructions=["Warm the gyros meat in a hot pan.", "Fill the pitas."])


@override_settings(OPENAI_API_KEY="test-key", AI_MODEL="gpt-5.4-mini")
class RecipeFromIdeaTests(TestCase):
    def setUp(self):
        freeze_now(self, date(2026, 10, 19))
        patcher = mock.patch.object(recipe_import, "start", side_effect=lambda job: recipe_import.run(job.pk))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email="pat@example.com")
        self.household = self.user.household
        self.household.pantry = "olive oil\noregano"
        self.household.save()
        approve_ai()
        self.client.force_login(self.user)
        FamilyMember.objects.create(household=self.household, name="Ana", allergies="Sesame")
        FamilyMember.objects.create(household=self.household, name="Ben")
        FamilyMember.objects.create(household=self.household, name="Leo", kind="child", birth_year=2019, dislikes="Onions")

    def _fake(self, *replies):
        fake = FakeOpenAI(*replies)
        patcher = mock.patch.object(planner, "get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _idea(self, idea=IDEA, **extra):
        return self.client.post(reverse("meals:recipe_idea_new"), {"idea": idea, **extra})

    def test_offered_first_on_the_add_page(self):
        page = self.client.get(reverse("meals:recipe_add")).content.decode()
        self.assertLess(page.index("💡 From an idea"), page.index("🔗 From a link"))

    def test_written_for_the_family_and_planned_for_the_day(self):
        fake = self._fake(reply(gyros_call()))
        response = self._idea(plan="2026-10-21:dinner", next="/week/2026-10-19/")
        job = RecipeImport.objects.get()
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]), fetch_redirect_response=False)
        self.assertEqual((job.status, job.idea, job.plan_date), ("done", IDEA, date(2026, 10, 21)))

        sent = fake.requests[0]
        self.assertEqual(sent["instructions"], recipe_import.IDEA_PROMPT)
        self.assertEqual([t.get("name", t["type"]) for t in sent["tools"]], ["ask_questions", "save_recipe"])  # no web search
        prompt = sent["input"][0]["content"][0]["text"]
        self.assertIn(f"The idea: {IDEA}", prompt)
        self.assertIn("Write it for 3 portions", prompt)
        self.assertIn("Ana (adult): ALLERGIES: Sesame", prompt)
        self.assertIn("Leo (child, 7): dislikes: Onions", prompt)
        self.assertIn("Usually at home: olive oil, oregano", prompt)
        self.assertEqual((job.result["recipe_url"], job.result["source"], job.result["servings"]), ("", "", 3))

        review = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(review, "The AI wrote this recipe from your idea")
        self.assertContains(review, f"💡 {IDEA}")
        self.assertContains(review, "Once saved, it's planned for Wednesday 21 Oct, dinner.")

        form = review.context["formset"]
        data = {"name": "Pork gyros pitas", "kind": "meat", "servings": "3", "status": "try", "recipe_url": "", "source": "",
                "minutes": "20", "instructions": "Warm it.\nFill the pitas.", "notes": "",
                "ingredients-TOTAL_FORMS": str(len(form.forms)), "ingredients-INITIAL_FORMS": "0",
                "ingredients-MIN_NUM_FORMS": "0", "ingredients-MAX_NUM_FORMS": "1000"}
        for row in form.forms:
            for name in ["name", "quantity", "unit", "category", "note"]:
                value = row[name].value()
                data[row.add_prefix(name)] = "" if value is None else str(value)
        response = self.client.post(reverse("meals:recipe_import", args=[job.pk]), data)
        self.assertRedirects(response, "/week/2026-10-19/", fetch_redirect_response=False)
        dish = Dish.objects.get(name="Pork gyros pitas")
        self.assertEqual((dish.status, dish.ingredients.count()), ("try", 3))
        meal = PlannedMeal.objects.get(dish=dish)
        self.assertEqual((meal.date, meal.slot), (date(2026, 10, 21), "dinner"))

    def test_questions_first(self):
        fake = self._fake(reply(questions_call("Which sauce would you like?", "Is it a main meal?", "Spicy?", "One too many?")),
                          reply(gyros_call()))
        self._idea()
        job = RecipeImport.objects.get()
        self.assertEqual(job.status, "questions")
        self.assertEqual(len(job.questions), 3)  # at most three
        self.assertContains(self.client.get(reverse("meals:recipes")), "Has a question for you")

        page = self.client.get(reverse("meals:recipe_import", args=[job.pk]))
        self.assertContains(page, '<label for="answer-0">Which sauce would you like?</label>')
        response = self.client.post(reverse("meals:recipe_import_answer", args=[job.pk]),
                                    {"answer-0": "Tzatziki", "answer-1": "", "answer-2": "  not  spicy "})
        self.assertRedirects(response, reverse("meals:recipe_import", args=[job.pk]), fetch_redirect_response=False)
        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(job.answers[2], {"question": "Spicy?", "answer": "not spicy"})

        second = fake.requests[1]
        self.assertEqual([t["name"] for t in second["tools"]], ["save_recipe"])  # no second round of questions
        prompt = second["input"][0]["content"][0]["text"]
        self.assertIn("- Which sauce would you like? Tzatziki", prompt)
        self.assertIn("- Is it a main meal? (no answer: decide yourself)", prompt)
        # Both calls count: in the ledger and on the job.
        self.assertEqual(AIUsage.objects.filter(kind="recipe").count(), 2)
        self.assertEqual(job.input_tokens, 2000)

    def test_needs_an_idea_and_ai(self):
        response = self._idea(idea="  ", plan="2026-10-21:dinner", next="/week/2026-10-19/")
        self.assertRedirects(response, reverse("meals:recipe_add") + "?plan=2026-10-21%3Adinner&next=%2Fweek%2F2026-10-19%2F",
                             fetch_redirect_response=False)
        self.household.ai_approved = False
        self.household.save()
        response = self.client.post(reverse("meals:recipe_idea_new"), {"idea": IDEA}, follow=True)
        self.assertContains(response, "once an admin has approved")
        self.assertFalse(RecipeImport.objects.exists())
        self.assertContains(self.client.get(reverse("meals:recipe_add")), "disabled>Create recipe</button>")

    def test_answers_only_for_own_questions(self):
        other = User.objects.create_user(email="sam@example.com").household
        job = RecipeImport.objects.create(household=other, idea="Soup", status="questions", questions=["Which?"])
        self.assertEqual(self.client.post(reverse("meals:recipe_import_answer", args=[job.pk]), {"answer-0": "x"}).status_code, 404)
