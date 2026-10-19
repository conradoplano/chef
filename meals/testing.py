"""Helpers for tests."""
from .models import Household


def home():
    """The household of the test's (first) user; created if there's none yet."""
    return Household.objects.order_by("pk").first() or Household.objects.create()


def approve_ai():
    """Lets the test's households use AI, like the family that was approved by an admin."""
    Household.objects.update(ai_approved=True)


def freeze_now(test, when):
    """Makes "now" (and today) `when` for the rest of the test, e.g. so the test week is this week."""
    from datetime import datetime, timezone
    from unittest import mock

    moment = when if isinstance(when, datetime) else datetime(when.year, when.month, when.day, 10, tzinfo=timezone.utc)
    patcher = mock.patch("django.utils.timezone.now", return_value=moment)
    patcher.start()
    test.addCleanup(patcher.stop)
