import re
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import User


class CodeLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="parent@example.com", name="Pat Parent")

    def _request_code(self, email="parent@example.com"):
        return self.client.post(reverse("accounts:login"), {"email": email})

    def _code_from_mail(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_home_requires_login(self):
        response = self.client.get(reverse("meals:home"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next=/")

    def test_known_email_receives_code_and_can_log_in(self):
        response = self._request_code("Parent@Example.com")
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.post(reverse("accounts:verify"), {"code": self._code_from_mail()})
        self.assertRedirects(response, reverse("meals:home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_unknown_email_gets_same_flow_but_no_mail(self):
        response = self._request_code("stranger@example.com")
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertEqual(len(mail.outbox), 0)
        response = self.client.post(reverse("accounts:verify"), {"code": "123456"})
        self.assertContains(response, "not correct")

    def test_wrong_code_rejected(self):
        self._request_code()
        code = self._code_from_mail()
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        response = self.client.post(reverse("accounts:verify"), {"code": wrong})
        self.assertContains(response, "not correct")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_attempts_lock_out(self):
        self._request_code()
        code = self._code_from_mail()
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        for _ in range(5):
            self.client.post(reverse("accounts:verify"), {"code": wrong})
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_is_single_use(self):
        self._request_code()
        code = self._code_from_mail()
        self.client.post(reverse("accounts:verify"), {"code": code})
        self.client.post(reverse("accounts:logout"))
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("accounts:login"))

    def test_inactive_user_cannot_log_in(self):
        self._request_code()
        code = self._code_from_mail()
        self.user.is_active = False
        self.user.save()
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertContains(response, "not correct")

    def test_mail_failure_shows_message(self):
        with mock.patch("accounts.views.send_mail", side_effect=OSError("smtp down")):
            response = self._request_code()
        self.assertContains(response, "couldn&#x27;t send the email")
        self.assertNotIn("pending_login", self.client.session)

    @override_settings(DEV_LOGIN=True)
    def test_dev_login_signs_in_without_code_or_mail(self):
        response = self._request_code()
        self.assertRedirects(response, reverse("meals:home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(DEV_LOGIN=True)
    def test_dev_login_rejects_unknown_email(self):
        response = self._request_code("stranger@example.com")
        self.assertContains(response, "No active user")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_dev_login_off_by_default(self):
        self._request_code()
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)

    def test_codes_per_address_are_limited(self):
        for _ in range(5):
            self._request_code()
        self.assertEqual(len(mail.outbox), 5)
        response = self._request_code()
        self.assertContains(response, "Too many codes")
        self.assertEqual(len(mail.outbox), 5)
        # Unknown addresses are limited the same way, so the limit reveals nothing.
        for _ in range(5):
            self._request_code("stranger@example.com")
        self.assertContains(self._request_code("stranger@example.com"), "Too many codes")

    def test_limit_resets_after_the_window(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import LoginCodeRequest

        for _ in range(5):
            self._request_code()
        LoginCodeRequest.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        self.assertRedirects(self._request_code(), reverse("accounts:verify"))

    def test_next_parameter_is_respected(self):
        self.client.post(f"{reverse('accounts:login')}?next=/shopping/", {"email": "parent@example.com"})
        response = self.client.post(reverse("accounts:verify"), {"code": self._code_from_mail()})
        self.assertRedirects(response, "/shopping/")
