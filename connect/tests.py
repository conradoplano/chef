import base64
import hashlib
import json
from datetime import date, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from meals.models import Dish, ExtraItem, Feedback, Household, MenuRequest, PlannedMeal, ShoppingCheck

from . import mcp
from .models import AuthorizationCode, Client, Connection

WEEK = date(2026, 10, 5)  # Monday
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class OAuthMixin:
    def register(self, **data):
        body = {"redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none", **data}
        return self.client.post(reverse("connect:register"), json.dumps(body), content_type="application/json")

    def authorize_url(self, client_id, **params):
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
                 "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "chef",
                 "resource": "http://testserver/mcp", **params}
        return reverse("connect:authorize") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def connect(self, user):
        """The whole flow an assistant goes through; returns the token response."""
        client_id = self.register().json()["client_id"]
        self.client.force_login(user)
        allowed = self.client.post(self.authorize_url(client_id), {"allow": "1"})
        code = parse_qs(urlsplit(allowed["Location"]).query)["code"][0]
        self.client.logout()
        return self.client.post(reverse("connect:token"), {
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id,
            "code_verifier": VERIFIER,
        }).json() | {"client_id": client_id}


class OAuthTests(OAuthMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com", name="Pat")

    def test_discovery(self):
        response = self.client.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"', response["WWW-Authenticate"])
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], "http://testserver/mcp")
        self.assertEqual(resource["authorization_servers"], ["http://testserver"])
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["issuer"], "http://testserver")
        self.assertEqual(server["registration_endpoint"], "http://testserver/oauth/register")
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])

    def test_full_flow_and_refresh(self):
        registered = self.register()
        self.assertEqual(registered.status_code, 201)
        client_id = registered.json()["client_id"]
        self.assertNotIn("client_secret", registered.json())

        # Not logged in: first the emailed-code login, then back here.
        url = self.authorize_url(client_id)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login") + "?next="))

        self.client.force_login(self.user)
        consent = self.client.get(url)
        self.assertContains(consent, "Connect Claude?")
        self.assertContains(consent, "claude.ai")
        allowed = self.client.post(url, {"allow": "1"})
        params = parse_qs(urlsplit(allowed["Location"]).query)
        self.assertTrue(allowed["Location"].startswith(REDIRECT + "?"))
        self.assertEqual(params["state"], ["xyz"])

        token_request = {"grant_type": "authorization_code", "code": params["code"][0], "redirect_uri": REDIRECT,
                         "client_id": client_id, "code_verifier": VERIFIER}
        tokens = self.client.post(reverse("connect:token"), token_request).json()
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["expires_in"], 3600)
        # Codes work once.
        self.assertEqual(self.client.post(reverse("connect:token"), token_request).json()["error"], "invalid_grant")

        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).json()["result"], {})

        refreshed = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}).json()
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        # The old tokens stop working.
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)
        old_refresh = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        self.assertEqual(old_refresh.json()["error"], "invalid_grant")
        self.assertEqual(Connection.objects.count(), 1)

    def test_pkce_is_required_and_checked(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        refused = self.client.get(self.authorize_url(client_id, code_challenge_method="plain"))
        self.assertIn("error=invalid_request", refused["Location"])
        allowed = self.client.post(self.authorize_url(client_id), {"allow": "1"})
        code = parse_qs(urlsplit(allowed["Location"]).query)["code"][0]
        wrong = self.client.post(reverse("connect:token"), {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                                            "redirect_uri": REDIRECT, "code_verifier": "w" * 64})
        self.assertEqual(wrong.json()["error"], "invalid_grant")

    def test_deny(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        denied = self.client.post(self.authorize_url(client_id), {"deny": "1"})
        self.assertIn("error=access_denied", denied["Location"])
        self.assertFalse(AuthorizationCode.objects.exists())

    def test_unknown_redirect_is_never_followed(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        response = self.client.get(self.authorize_url(client_id, redirect_uri="https://evil.example.com/cb"))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Can't connect this app", status_code=400)

    def test_registration_rules(self):
        self.assertEqual(self.register(redirect_uris=["http://example.com/cb"]).json()["error"], "invalid_redirect_uri")
        self.assertEqual(self.register(redirect_uris=["http://localhost:6274/cb"]).status_code, 201)
        confidential = self.register(token_endpoint_auth_method="client_secret_post").json()
        self.assertIn("client_secret", confidential)
        bad = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "client_id": confidential["client_id"], "client_secret": "nope"})
        self.assertEqual(bad.status_code, 401)
        with mock.patch("connect.oauth.REGISTRATIONS_PER_HOUR", 2):  # two registered above
            self.assertEqual(self.register().status_code, 429)

    def test_disconnect_in_settings(self):
        tokens = self.connect(self.user)
        self.client.force_login(self.user)
        page = self.client.get(reverse("meals:family"))
        self.assertContains(page, "http://testserver/mcp")
        self.assertContains(page, "Claude")
        connection = Connection.objects.get()
        other = User.objects.create_user(email="sam@example.com")
        self.client.force_login(other)  # another household can't
        self.assertEqual(self.client.post(reverse("connect:connection_remove", args=[connection.pk])).status_code, 404)
        self.client.force_login(self.user)
        self.client.post(reverse("connect:connection_remove", args=[connection.pk]))
        response = self.client.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(response.status_code, 401)
        self.assertIn('error="invalid_token"', response["WWW-Authenticate"])

    def test_suspended_household_and_expired_token(self):
        tokens = self.connect(self.user)
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
        Household.objects.update(is_active=False)
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)
        Household.objects.update(is_active=True)
        Connection.objects.update(access_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)

    def test_revoke_endpoint(self):
        tokens = self.connect(self.user)
        self.client.post(reverse("connect:revoke"), {"token": tokens["refresh_token"], "client_id": tokens["client_id"]})
        self.assertFalse(Connection.objects.exists())

    def test_unused_registrations_are_cleaned_up(self):
        self.register()
        Client.objects.update(created_at=timezone.now() - timedelta(days=2))
        self.register()
        self.assertEqual(Client.objects.count(), 1)


class MCPTests(OAuthMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com", name="Pat")
        self.household = self.user.household
        self.token = self.connect(self.user)["access_token"]
        self.lasagne = Dish.objects.create(household=self.household, name="Lasagne", status="favourite", minutes=60,
                                           instructions="Layer it.\nBake it.")
        self.lasagne.ingredients.create(name="Mince", quantity=500, unit="g", category="meat")
        self.curry = Dish.objects.create(household=self.household, name="Thai green curry", status="try")
        self.curry.ingredients.create(name="Coconut milk", quantity=1, unit="can", category="pantry")
        self.monday = PlannedMeal.objects.create(household=self.household, date=WEEK, slot="dinner", dish=self.lasagne)

    def rpc(self, method, params=None, message_id=1):
        body = {"jsonrpc": "2.0", "id": message_id, "method": method, **({"params": params} if params is not None else {})}
        return self.client.post("/mcp", body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def call(self, tool, **arguments):
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments}).json()["result"]
        if result.get("isError"):
            return result["content"][0]["text"]
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def test_handshake(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}).json()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])
        notified = self.client.post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(notified.status_code, 202)
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)
        self.assertEqual(self.client.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405)

    def test_exactly_these_tools(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [
            "get_week_menu", "plan_meal", "remove_meal", "copy_week", "get_shopping_list", "add_item", "remove_item",
            "tick_item", "search_recipes", "get_recipe", "rate_meal",
        ])
        by_name = {t["name"]: t for t in tools}
        self.assertTrue(by_name["remove_meal"]["annotations"]["destructiveHint"])
        self.assertTrue(by_name["get_week_menu"]["annotations"]["readOnlyHint"])
        self.assertEqual(by_name["plan_meal"]["inputSchema"]["required"], ["date", "slot", "recipe"])

    def test_week_menu(self):
        MenuRequest.objects.create(household=self.household, week=WEEK, kind="create", status="done", summary="A cosy week.", slots=[])
        menu = self.call("get_week_menu", week="2026-10-08")
        self.assertEqual(menu["week_start"], "2026-10-05")
        self.assertEqual(menu["about_this_menu"], "A cosy week.")
        monday = menu["days"][0]
        self.assertEqual((monday["weekday"], monday["meals"][0]["dish"], monday["meals"][0]["meal_id"]), ("Monday", "Lasagne", self.monday.pk))
        self.assertEqual(menu["days"][1]["meals"], [])

    def test_plan_meal(self):
        planned = self.call("plan_meal", date="2026-10-06", slot="dinner", recipe="thai green CURRY")
        self.assertEqual((planned["planned"]["dish"], planned["new_dish"]), ("Thai green curry", False))
        by_id = self.call("plan_meal", date="2026-10-07", slot="lunch", recipe=str(self.lasagne.pk), leftovers=True)
        self.assertTrue(by_id["planned"]["leftovers"])
        new = self.call("plan_meal", date="2026-10-08", slot="dinner", recipe="Pancakes")
        self.assertTrue(new["new_dish"])
        self.assertIn("no ingredients", new["note"])
        self.assertEqual(PlannedMeal.objects.get(date="2026-10-08").updated_by, self.user)
        self.assertIn("slot", self.call("plan_meal", date="2026-10-08", slot="breakfast", recipe="Toast"))
        self.assertIn("date like", self.call("plan_meal", date="tomorrow", slot="dinner", recipe="Toast"))

    def test_remove_and_copy(self):
        leftover = PlannedMeal.objects.create(household=self.household, date=WEEK + timedelta(days=2), slot="lunch", dish=self.lasagne, leftovers=True)
        copied = self.call("copy_week", from_week="2026-10-05", to_week="2026-10-12")
        self.assertEqual((copied["copied"], copied["skipped_already_planned"]), (2, 0))
        removed = self.call("remove_meal", meal_id=self.monday.pk)
        self.assertEqual(removed["leftovers_removed"], ["Wednesday lunch"])
        self.assertFalse(PlannedMeal.objects.filter(pk__in=[self.monday.pk, leftover.pk]).exists())
        self.assertIn("no meal", self.call("remove_meal", meal_id=self.monday.pk))

    def test_shopping_list(self):
        listing = self.call("get_shopping_list", week="2026-10-05")
        [section] = listing["sections"]
        self.assertEqual(section["items"][0]["name"], "Mince")
        self.assertEqual(section["items"][0]["for_meals"], ["Mon · Lasagne"])

        added = self.call("add_item", name="Oat milk", quantity="2 l", category="dairy", week="2026-10-05")
        self.assertEqual(ExtraItem.objects.get().created_by, self.user)
        self.assertEqual(self.call("tick_item", item="oat milk", week="2026-10-05")["bought"], True)
        self.assertEqual(self.call("tick_item", item="mince", week="2026-10-05")["item"], "mince")
        self.assertEqual(ShoppingCheck.objects.count(), 2)
        self.call("tick_item", item="Mince", bought=False, week="2026-10-05")
        self.assertEqual(ShoppingCheck.objects.count(), 1)

        self.assertIn("Tick it as bought instead", self.call("remove_item", item="Mince", week="2026-10-05"))
        self.assertEqual(self.call("remove_item", item=added["added"]["item"], week="2026-10-05")["removed"], "Oat milk")
        self.assertIn("isn't on the shopping list", self.call("tick_item", item="Caviar", week="2026-10-05"))
        self.assertIn("category must be one of", self.call("add_item", name="X", category="toys"))

    def test_recipes(self):
        found = self.call("search_recipes", query="coconut")
        self.assertEqual([r["name"] for r in found["recipes"]], ["Thai green curry"])
        everything = self.call("search_recipes")
        self.assertEqual([r["name"] for r in everything["recipes"]], ["Lasagne", "Thai green curry"])  # favourites first
        self.assertEqual(self.call("search_recipes", binder="try")["count"], 1)
        recipe = self.call("get_recipe", recipe_id=self.lasagne.pk)
        self.assertEqual(recipe["method"], ["Layer it.", "Bake it."])
        self.assertEqual(recipe["ingredients"][0], {"name": "Mince", "quantity": 500.0, "unit": "g", "section": "meat", "note": None})
        self.assertEqual(recipe["open_in_chef"], f"http://testserver/recipes/{self.lasagne.pk}/")

    def test_rate_meal(self):
        with mock.patch("django.utils.timezone.localdate", return_value=WEEK + timedelta(days=1)):
            cooked = PlannedMeal.objects.create(household=self.household, date=WEEK, slot="lunch", dish=self.curry)
            rated = self.call("rate_meal", meal_id=cooked.pk, kids="loved", parents="loved")
            self.assertTrue(rated["became_favourite"])
            self.call("rate_meal", meal_id=cooked.pk, notes="Less chili next time")
            review = Feedback.objects.get(meal=cooked)
            self.assertEqual((review.kids, review.parents, review.notes, review.updated_by), ("loved", "loved", "Less chili next time", self.user))
            later = PlannedMeal.objects.create(household=self.household, date=WEEK + timedelta(days=3), dish=self.curry)
            self.assertIn("once it has been eaten", self.call("rate_meal", meal_id=later.pk, kids="okay"))
            leftover = PlannedMeal.objects.create(household=self.household, date=WEEK + timedelta(days=1), dish=self.curry, leftovers=True)
            self.assertIn(f"rate meal {cooked.pk}", self.call("rate_meal", meal_id=leftover.pk, kids="okay"))
            self.assertIn("at least one", self.call("rate_meal", meal_id=cooked.pk))

    def test_only_their_own_household(self):
        other = User.objects.create_user(email="sam@example.com").household
        secret = Dish.objects.create(household=other, name="Secret stew")
        theirs = PlannedMeal.objects.create(household=other, date=WEEK, dish=secret)
        self.assertIn("no meal", self.call("remove_meal", meal_id=theirs.pk))
        self.assertIn("no meal", self.call("rate_meal", meal_id=theirs.pk, kids="loved"))
        self.assertIn("no recipe", self.call("get_recipe", recipe_id=secret.pk))
        self.assertNotIn("Secret stew", json.dumps(self.call("search_recipes")))
        self.assertNotIn("Secret stew", json.dumps(self.call("get_week_menu", week="2026-10-05")))
        planned = self.call("plan_meal", date="2026-10-06", slot="dinner", recipe=str(secret.pk))
        self.assertTrue(planned["new_dish"])  # not their stew: a new dish called "<id>"
        self.assertTrue(PlannedMeal.objects.filter(pk=theirs.pk).exists())

    def test_rate_limit(self):
        with mock.patch.object(mcp, "CALLS_PER_MINUTE", 2):
            self.rpc("ping")
            self.rpc("ping")
            self.assertEqual(self.rpc("ping").status_code, 429)
