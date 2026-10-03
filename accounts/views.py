"""
Passwordless login with a six-digit code sent by email.

A code (rather than a link) keeps the login inside whatever app or browser the
user started in, which matters when the site is installed on the home screen.
The pending login lives in the session, so a code only works in the browser
that requested it.
"""
import logging
import secrets
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import CodeForm, EmailLoginForm
from .models import User

logger = logging.getLogger(__name__)

SESSION_KEY = "pending_login"
MAX_ATTEMPTS = 5


def _hash(code):
    return salted_hmac("accounts.login-code", code).hexdigest()


def login_request(request):
    """Ask for an email address and send a login code to it."""
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    form = EmailLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        next_url = request.GET.get("next", "")
        if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            next_url = ""

        if settings.DEV_LOGIN:
            if user is None:
                form.add_error("email", "No active user with this email.")
                return render(request, "accounts/login.html", {"form": form})
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)

        code = f"{secrets.randbelow(10**6):06d}"
        # Store state even for unknown emails so the flow looks identical.
        request.session[SESSION_KEY] = {
            "uid": user.pk if user else None,
            "hash": _hash(code),
            "expires": time.time() + settings.LOGIN_CODE_MAX_AGE,
            "attempts": 0,
            "next": next_url,
        }
        if user:
            try:
                _send_code(user, code)
            except Exception:
                logger.exception("Could not send login code to %s", email)
                del request.session[SESSION_KEY]
                messages.error(request, "We couldn't send the email right now. Please try again later.")
                return render(request, "accounts/login.html", {"form": form})
        else:
            logger.info("Login requested for unknown email %s", email)
        return redirect("accounts:verify")

    return render(request, "accounts/login.html", {"form": form})


def _send_code(user, code):
    context = {
        "user": user,
        "code": code,
        "minutes": settings.LOGIN_CODE_MAX_AGE // 60,
        "site_name": settings.SITE_NAME,
    }
    send_mail(
        subject=f"Your {settings.SITE_NAME} login code: {code}",
        message=render_to_string("accounts/email/login_code.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )


def login_verify(request):
    state = request.session.get(SESSION_KEY)
    if not state:
        return redirect("accounts:login")

    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if time.time() > state["expires"] or state["attempts"] >= MAX_ATTEMPTS:
            del request.session[SESSION_KEY]
            messages.error(request, "That code has expired. Please request a new one.")
            return redirect("accounts:login")

        state["attempts"] += 1
        request.session[SESSION_KEY] = state

        user = User.objects.filter(pk=state["uid"], is_active=True).first() if state["uid"] else None
        if user and constant_time_compare(_hash(form.cleaned_data["code"]), state["hash"]):
            next_url = state["next"]
            del request.session[SESSION_KEY]
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)

        form.add_error("code", "That code is not correct.")

    return render(request, "accounts/verify.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)
