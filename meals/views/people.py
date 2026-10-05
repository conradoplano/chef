"""The people who use the app together as a household: inviting, removing, naming the household."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import User

from ..forms import HouseholdNameForm, InviteForm
from ..models import Household
from ..notify import site_url

logger = logging.getLogger(__name__)


def people_url():
    return reverse("meals:family") + "#people"


@login_required
@require_POST
def person_invite(request):
    """Adds someone to the household. They log in with their email; no password, no confirmation."""
    household = request.household
    form = InviteForm(request.POST, prefix="invite")
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(people_url())
    email, name = form.cleaned_data["email"], form.cleaned_data["name"]
    existing = User.objects.filter(email__iexact=email).first()
    if existing and existing.household_id == household.pk:
        messages.info(request, f"{existing} is already in your household.")
    elif existing:
        # One household per email: moving someone would take them away from their own family's data.
        messages.error(request, f"{email} already uses {settings.SITE_NAME} with another household.")
    else:
        person = User.objects.create_user(email=email, name=name, household=household)
        sent = send_invite(person, request.user)
        messages.success(
            request,
            f"Added {person}. " + ("They've been sent an email; " if sent else "")
            + "they log in with their email address.",
        )
    return redirect(people_url())


def send_invite(person, inviter):
    """Tells the new person how to log in. Returns False if the email couldn't be sent."""
    context = {
        "person": person,
        "inviter": inviter.get_short_name(),
        "household": person.household,
        "url": site_url() + reverse("accounts:login") if site_url() else "",
        "site_name": settings.SITE_NAME,
    }
    try:
        send_mail(
            subject=f"{inviter.get_short_name()} added you to {settings.SITE_NAME}",
            message=render_to_string("meals/email/invite.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[person.email],
        )
        return True
    except Exception:
        logger.exception("Could not send the invitation to %s", person.email)
        return False


@login_required
@require_POST
def person_remove(request, pk):
    """Removes someone from the household (yourself too: leaving). The last person can't be removed."""
    household = request.household
    person = get_object_or_404(User, pk=pk, household=household)
    if household.members.count() <= 1:
        messages.error(request, "You're the only one in this household, so you can't be removed.")
        return redirect(people_url())
    leaving = person.pk == request.user.pk
    if person.is_staff:
        # Admins keep their account (and the admin pages), in a household of their own.
        person.household = Household.objects.create(ai_approved=True)
        person.save(update_fields=["household"])
    else:
        person.delete()
    if leaving:
        logout(request)
        messages.success(request, f"You've left {household}.")
        return redirect("accounts:login")
    messages.success(request, f"Removed {person} from the household.")
    return redirect(people_url())


@login_required
@require_POST
def household_rename(request):
    form = HouseholdNameForm(request.POST, instance=request.household, prefix="household")
    if form.is_valid():
        form.save()
        messages.success(request, "Household name saved.")
    return redirect(people_url())
