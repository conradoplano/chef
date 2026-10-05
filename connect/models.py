"""OAuth for assistants (Claude, ChatGPT...) that use Chef through MCP: the apps that registered,
the codes of logins in progress, and the connections people approved, with their tokens.
Codes and tokens are only stored as SHA-256 hashes."""
import hashlib
import secrets

from django.conf import settings
from django.db import models


def new_secret(prefix):
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Client(models.Model):
    """An app that registered itself (OAuth dynamic client registration). Registering grants nothing:
    a person still has to log in and allow it."""

    client_id = models.CharField(max_length=64, unique=True)
    secret_hash = models.CharField(max_length=64, blank=True, help_text="Empty for public clients (PKCE only).")
    name = models.CharField(max_length=200, blank=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    created_ip = models.GenericIPAddressField(null=True, blank=True)

    def __str__(self):
        return self.name or self.client_id


class AuthorizationCode(models.Model):
    """Handed to the app after a person allowed it; exchanged once for tokens, within minutes."""

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    redirect_uri = models.CharField(max_length=500)
    code_challenge = models.CharField(max_length=128)
    resource = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()


class Connection(models.Model):
    """An app a person allowed to use their household's data, with its current tokens.
    Refreshing replaces both tokens; removing the connection (Settings) cuts the app off."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="connections")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connections")
    access_hash = models.CharField(max_length=64, unique=True)
    access_expires_at = models.DateTimeField()
    refresh_hash = models.CharField(max_length=64, unique=True)
    refresh_expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Calls in the current minute, to stop a runaway assistant.
    window_start = models.DateTimeField(null=True, blank=True)
    window_calls = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.client} for {self.user}"
