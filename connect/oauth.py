"""
OAuth 2.1 for assistants that use Chef through MCP (see mcp.py).

An assistant finds everything from the MCP address: a request without a token gets a 401 that points
to the protected resource metadata (RFC 9728), which names this site as the authorization server; its
metadata (RFC 8414) lists the endpoints. The assistant registers itself (RFC 7591), sends the person to
/oauth/authorize - log in with the emailed code, then allow it - and swaps the code for tokens (PKCE
with S256 is required). Access tokens last an hour; refresh tokens 60 days and are replaced on use.
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import AuthorizationCode, Client, Connection, digest, new_secret

SCOPE = "chef"
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=60)
CODE_LIFETIME = timedelta(minutes=10)
REGISTRATIONS_PER_HOUR = 20  # per IP address
UNUSED_CLIENT_LIFETIME = timedelta(days=1)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def base_url(request):
    return f"{request.scheme}://{request.get_host()}"


def mcp_url(request):
    return base_url(request) + reverse("connect:mcp")


def client_ip(request):
    """The person's address; behind the NAS's reverse proxy, the one it passes on."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def cors(view):
    """Lets browser-based MCP clients read the metadata and use the endpoints (no cookies are involved)."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = view(request, *args, **kwargs)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id"
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
        return response
    return wrapped


def oauth_error(error, description="", status=400):
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    response["Cache-Control"] = "no-store"
    return response


# --- discovery -------------------------------------------------------------------------------


@cors
def protected_resource(request):
    return JsonResponse({
        "resource": mcp_url(request),
        "authorization_servers": [base_url(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": settings.SITE_NAME,
    })


@cors
def authorization_server(request):
    base = base_url(request)
    return JsonResponse({
        "issuer": base,
        "authorization_endpoint": base + reverse("connect:authorize"),
        "token_endpoint": base + reverse("connect:token"),
        "registration_endpoint": base + reverse("connect:register"),
        "revocation_endpoint": base + reverse("connect:revoke"),
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "service_documentation": base + reverse("meals:family") + "#assistants",
    })


# --- registration ----------------------------------------------------------------------------


def allowed_redirect(uri):
    """https addresses, or http on this computer (desktop apps); never fragments or other schemes."""
    parts = urlsplit(uri) if isinstance(uri, str) else None
    if not parts or not parts.hostname or parts.fragment or len(uri) > 500:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)


@csrf_exempt
@cors
def register(request):
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return oauth_error("invalid_client_metadata", "The body must be JSON.")
    if not isinstance(data, dict):
        return oauth_error("invalid_client_metadata", "The body must be a JSON object.")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 10 or not all(allowed_redirect(u) for u in uris):
        return oauth_error("invalid_redirect_uri", "Give 1-10 redirect_uris: https, or http on localhost.")
    method = data.get("token_endpoint_auth_method", "none")
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return oauth_error("invalid_client_metadata", "Unsupported token_endpoint_auth_method.")
    grants = data.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        return oauth_error("invalid_client_metadata", "Only authorization_code and refresh_token are supported.")

    now = timezone.now()
    ip = client_ip(request)
    if Client.objects.filter(created_ip=ip, created_at__gte=now - timedelta(hours=1)).count() >= REGISTRATIONS_PER_HOUR:
        return oauth_error("too_many_requests", "Too many registrations; try again later.", status=429)
    # Registrations nobody ever used are cleaned up.
    Client.objects.filter(created_at__lt=now - UNUSED_CLIENT_LIFETIME, connections=None, authorizationcode=None).delete()

    secret = secrets.token_urlsafe(32) if method != "none" else ""
    client = Client.objects.create(
        client_id=secrets.token_urlsafe(18), secret_hash=digest(secret) if secret else "",
        name=str(data.get("client_name", ""))[:200], redirect_uris=uris, created_ip=ip,
    )
    body = {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.name,
        "redirect_uris": uris,
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "scope": SCOPE,
    }
    if secret:
        body.update(client_secret=secret, client_secret_expires_at=0)
    response = JsonResponse(body, status=201)
    response["Cache-Control"] = "no-store"
    return response


# --- authorization: log in, then allow --------------------------------------------------------


def with_params(uri, **params):
    return uri + ("&" if "?" in uri else "?") + urlencode({k: v for k, v in params.items() if v})


def authorize(request):
    """Asks the logged-in person whether the app may use their household's data."""
    params = request.GET
    client = Client.objects.filter(client_id=params.get("client_id", "")).first()
    redirect_uri = params.get("redirect_uri") or (client.redirect_uris[0] if client and len(client.redirect_uris) == 1 else "")
    if client is None or redirect_uri not in client.redirect_uris:
        # Never send anyone to an address the app didn't register.
        return render(request, "connect/authorize_error.html", status=400)
    state = params.get("state", "")
    issuer = base_url(request)

    def refuse(error, description=""):
        return HttpResponseRedirect(with_params(redirect_uri, error=error, error_description=description, state=state, iss=issuer))

    if params.get("response_type") != "code":
        return refuse("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge", "")
    if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        return refuse("invalid_request", "PKCE with code_challenge_method=S256 is required.")
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())

    if request.method == "POST":
        if "allow" not in request.POST:
            return refuse("access_denied", "The person didn't allow it.")
        code = new_secret("chef_code")
        AuthorizationCode.objects.create(
            code_hash=digest(code), client=client, user=request.user, redirect_uri=redirect_uri,
            code_challenge=challenge, resource=params.get("resource", "")[:500],
            expires_at=timezone.now() + CODE_LIFETIME,
        )
        return HttpResponseRedirect(with_params(redirect_uri, code=code, state=state, iss=issuer))

    return render(request, "connect/authorize.html", {
        "client": client,
        "app_name": client.name or "An app",
        "redirect_host": urlsplit(redirect_uri).hostname,
    })


# --- tokens ----------------------------------------------------------------------------------


def authenticated_client(request):
    """The client making a token request, checking its secret if it has one; None if that fails."""
    client_id, secret = request.POST.get("client_id", ""), request.POST.get("client_secret", "")
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            client_id, _, secret = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return None
    client = Client.objects.filter(client_id=client_id).first()
    if client is None or (client.secret_hash and not constant_time_compare(digest(secret), client.secret_hash)):
        return None
    return client


def household_ok(user):
    return user.is_active and user.household_id and user.household.is_active


def issue(connection):
    """New access and refresh tokens for the connection (saved), as the token response."""
    access, refresh = new_secret("chef_at"), new_secret("chef_rt")
    now = timezone.now()
    connection.access_hash, connection.access_expires_at = digest(access), now + ACCESS_LIFETIME
    connection.refresh_hash, connection.refresh_expires_at = digest(refresh), now + REFRESH_LIFETIME
    connection.save()
    response = JsonResponse({
        "access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "refresh_token": refresh, "scope": SCOPE,
    })
    response["Cache-Control"] = "no-store"
    return response


def pkce_matches(verifier, challenge):
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return 43 <= len(verifier) <= 128 and constant_time_compare(computed, challenge)


@csrf_exempt
@cors
def token(request):
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    client = authenticated_client(request)
    if client is None:
        return oauth_error("invalid_client", "Unknown client or wrong secret.", status=401)
    grant = request.POST.get("grant_type")
    now = timezone.now()

    if grant == "authorization_code":
        code = AuthorizationCode.objects.select_related("user__household").filter(
            code_hash=digest(request.POST.get("code", "")), client=client
        ).first()
        if code is None:
            return oauth_error("invalid_grant", "Unknown or used code.")
        code.delete()  # single use, whatever happens next
        if code.expires_at < now or code.redirect_uri != request.POST.get("redirect_uri", code.redirect_uri):
            return oauth_error("invalid_grant", "The code has expired or the redirect_uri doesn't match.")
        if not pkce_matches(request.POST.get("code_verifier", ""), code.code_challenge):
            return oauth_error("invalid_grant", "The code_verifier doesn't match.")
        if not household_ok(code.user):
            return oauth_error("invalid_grant", "This account can't be used.")
        return issue(Connection(client=client, user=code.user))

    if grant == "refresh_token":
        connection = Connection.objects.select_related("user__household").filter(
            refresh_hash=digest(request.POST.get("refresh_token", "")), client=client, refresh_expires_at__gt=now
        ).first()
        if connection is None or not household_ok(connection.user):
            return oauth_error("invalid_grant", "Unknown or expired refresh token.")
        return issue(connection)

    return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


@csrf_exempt
@cors
def revoke(request):
    """The app disconnects itself (RFC 7009): either of its tokens ends the connection."""
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    client = authenticated_client(request)
    if client is None:
        return oauth_error("invalid_client", status=401)
    value = digest(request.POST.get("token", ""))
    Connection.objects.filter(client=client).filter(access_hash=value).delete()
    Connection.objects.filter(client=client).filter(refresh_hash=value).delete()
    return HttpResponse(status=200)


def authenticate(request):
    """The connection of a valid Bearer access token, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    connection = Connection.objects.select_related("user__household", "client").filter(
        access_hash=digest(header[7:].strip()), access_expires_at__gt=timezone.now()
    ).first()
    return connection if connection and household_ok(connection.user) else None


# --- managed in Settings ------------------------------------------------------------------------


@login_required
@require_POST
def connection_remove(request, pk):
    """Anyone in the household can disconnect an app, like they can remove people."""
    connection = get_object_or_404(Connection, pk=pk, user__household=request.household)
    connection.delete()
    messages.success(request, f"Disconnected {connection.client}.")
    return redirect(reverse("meals:family") + "#assistants")
