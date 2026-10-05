"""Helpers for tests."""
from .models import Household


def home():
    """The household of the test's (first) user; created if there's none yet."""
    return Household.objects.order_by("pk").first() or Household.objects.create()


def approve_ai():
    """Lets the test's households use AI, like the family that was approved by an admin."""
    Household.objects.update(ai_approved=True)
