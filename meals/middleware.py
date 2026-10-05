from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from .models import Household


class HouseholdMiddleware:
    """Sets request.household: the household of the logged-in user, whose data every page shows.
    A user without one gets a new household; a suspended household is logged out."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.household = None
        user = request.user
        if user.is_authenticated:
            if user.household_id is None:
                user.household = Household.objects.create()
                user.save(update_fields=["household"])
            if not user.household.is_active:
                logout(request)
                messages.error(request, "This household's account has been suspended.")
                return redirect("accounts:login")
            request.household = user.household
        return self.get_response(request)
