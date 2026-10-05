"""
Passwordless login with a six-digit code sent by email.

A code (rather than a link) keeps the login inside whatever app or browser the
user started in, which matters when the site is installed on the home screen.
The pending login lives in the session, so a code only works in the browser
that requested it. Registering works the same way: the household and account are
only created once the code proves the email address.
"""
import logging
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from meals import notify
from meals.models import Household

from .forms import CodeForm, EmailLoginForm, RegisterForm
from .models import LoginCodeRequest, User

logger = logging.getLogger(__name__)

SESSION_KEY = "pending_login"
MAX_ATTEMPTS = 5
# At most this many codes per email address in the window, so nobody can flood an inbox.
MAX_CODES = 5
CODES_WINDOW = timedelta(minutes=15)


def _too_many_codes(email):
    """Counts this request; True if the address already had MAX_CODES in the window.
    Unknown addresses are counted the same way, so the limit reveals nothing."""
    now = timezone.now()
    LoginCodeRequest.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
    if LoginCodeRequest.objects.filter(email=email, created_at__gte=now - CODES_WINDOW).count() >= MAX_CODES:
        return True
    LoginCodeRequest.objects.create(email=email)
    return False


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

        if _too_many_codes(email):
            logger.warning("Too many login codes requested for %s", email)
            messages.error(request, "Too many codes were requested for this address. Please wait a few minutes.")
            return render(request, "accounts/login.html", {"form": form})

        # Store state even for unknown emails so the flow looks identical.
        code = _pending(request, uid=user.pk if user else None, next_url=next_url)
        if user:
            if not _send_code(request, email, user.get_short_name(), code):
                return render(request, "accounts/login.html", {"form": form})
        else:
            logger.info("Login requested for unknown email %s", email)
        return redirect("accounts:verify")

    return render(request, "accounts/login.html", {"form": form})


def _pending(request, uid=None, register=None, next_url=""):
    """Remembers the login (or registration) waiting for its code; returns the code."""
    code = f"{secrets.randbelow(10**6):06d}"
    request.session[SESSION_KEY] = {
        "uid": uid,
        "register": register,
        "hash": _hash(code),
        "expires": time.time() + settings.LOGIN_CODE_MAX_AGE,
        "attempts": 0,
        "next": next_url,
    }
    return code


def _send_code(request, email, name, code):
    """Emails the code. Returns False (with a message for the user) if that failed."""
    context = {
        "name": name,
        "code": code,
        "minutes": settings.LOGIN_CODE_MAX_AGE // 60,
        "site_name": settings.SITE_NAME,
    }
    try:
        send_mail(
            subject=f"Your {settings.SITE_NAME} login code: {code}",
            message=render_to_string("accounts/email/login_code.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )
        return True
    except Exception:
        logger.exception("Could not send login code to %s", email)
        del request.session[SESSION_KEY]
        messages.error(request, "We couldn't send the email right now. Please try again later.")
        return False


def register(request):
    """A new household: the person registering, their name and the household's name.
    They can use the app straight away; AI once an admin has approved the household."""
    if not settings.REGISTRATION_OPEN:
        raise Http404("Registration is closed")
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        email = data["email"]
        user = User.objects.filter(email__iexact=email).first()

        if settings.DEV_LOGIN:
            user = user or create_household(data)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("meals:family")

        if _too_many_codes(email):
            logger.warning("Too many codes requested for %s", email)
            messages.error(request, "Too many codes were requested for this address. Please wait a few minutes.")
            return render(request, "accounts/register.html", {"form": form})
        if user is None:
            code = _pending(request, register=data)
            if not _send_code(request, email, data["name"].split(" ")[0], code):
                return render(request, "accounts/register.html", {"form": form})
        elif user.is_active:
            # Already registered: the code simply logs them in.
            code = _pending(request, uid=user.pk)
            if not _send_code(request, email, user.get_short_name(), code):
                return render(request, "accounts/register.html", {"form": form})
        else:
            _pending(request)
        return redirect("accounts:verify")

    return render(request, "accounts/register.html", {"form": form})


@transaction.atomic
def create_household(data):
    """The household and its first member, from the registration form. Tells the admins."""
    household = Household.objects.create(name=data["household"] or f"{data['name'].split(' ')[0]}'s household")
    user = User.objects.create_user(email=data["email"], name=data["name"], household=household)
    transaction.on_commit(lambda: notify.new_household(user))
    return user


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

        user = None
        if constant_time_compare(_hash(form.cleaned_data["code"]), state["hash"]):
            registering = state.get("register")
            if registering:
                user = User.objects.filter(email__iexact=registering["email"]).first() or create_household(registering)
            elif state["uid"]:
                user = User.objects.filter(pk=state["uid"]).first()
        if user and user.is_active:
            next_url = state["next"]
            del request.session[SESSION_KEY]
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if state.get("register"):
                messages.success(request, "Welcome! Start by adding your family and your usual week; then plan your first menu.")
                return redirect("meals:family")
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)

        form.add_error("code", "That code is not correct.")

    return render(request, "accounts/verify.html", {"form": form, "registering": bool(state.get("register"))})


@require_POST
def logout_view(request):
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)
