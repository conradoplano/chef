"""Emails the rest of the family when the AI has created or changed a week's menu."""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from accounts.models import User

from .models import MenuRequest, PlannedMeal

logger = logging.getLogger(__name__)


def site_url():
    """Where the app is reached: SITE_URL, else the first trusted origin (e.g. https://chef.example.com)."""
    return (settings.SITE_URL or next(iter(settings.CSRF_TRUSTED_ORIGINS), "")).rstrip("/")


def menu_ready(request):
    """Tells everyone except the person who asked. Failures are logged, never raised."""
    if not settings.MENU_EMAILS:
        return
    recipients = list(
        User.objects.filter(is_active=True).exclude(pk=request.created_by_id).exclude(email="")
        .values_list("email", flat=True)
    )
    if not recipients:
        return
    slots = {(s["date"], s["slot"]) for s in request.slots}
    meals = [
        m for m in PlannedMeal.objects.filter(date__range=(request.week, request.week + timedelta(days=6)))
        .select_related("dish")
        if (m.date.isoformat(), m.slot) in slots
    ]
    who = request.created_by.get_short_name() if request.created_by else "Someone"
    week = f"{request.week.day} {request.week:%b}"
    subject = {
        MenuRequest.Kind.CREATE: f"New menu for the week of {week}",
        MenuRequest.Kind.CHANGE: f"Menu changed for the week of {week}",
        MenuRequest.Kind.REPLACE: f"{request.replacing} was replaced on the menu",
    }[request.kind]
    context = {
        "request": request,
        "who": who,
        "meals": meals,
        "url": site_url() + reverse("meals:menu_of", args=[request.week.isoformat()]) if site_url() else "",
        "site_name": settings.SITE_NAME,
    }
    try:
        send_mail(
            subject=f"{settings.SITE_NAME}: {subject}",
            message=render_to_string("meals/email/menu_ready.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
        )
    except Exception:
        logger.exception("Could not send the menu email for request %s", request.pk)
