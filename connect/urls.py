from django.urls import path

from . import mcp, oauth

app_name = "connect"

urlpatterns = [
    path("mcp", mcp.mcp, name="mcp"),
    # Clients look for the metadata with and without the resource's path.
    path(".well-known/oauth-protected-resource", oauth.protected_resource, name="protected_resource"),
    path(".well-known/oauth-protected-resource/mcp", oauth.protected_resource),
    path(".well-known/oauth-authorization-server", oauth.authorization_server, name="authorization_server"),
    path(".well-known/oauth-authorization-server/mcp", oauth.authorization_server),
    path("oauth/register", oauth.register, name="register"),
    path("oauth/authorize", oauth.authorize, name="authorize"),
    path("oauth/token", oauth.token, name="token"),
    path("oauth/revoke", oauth.revoke, name="revoke"),
    path("settings/assistants/<int:pk>/remove/", oauth.connection_remove, name="connection_remove"),
]
