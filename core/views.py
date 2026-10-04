import os

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control


def health(request):
    """Unauthenticated health check for Docker / the NAS."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return HttpResponse("db unavailable", status=503)
    return JsonResponse({"status": "ok", "version": os.environ.get("APP_VERSION", "dev")[:7]})


@cache_control(max_age=86400)
def manifest(request):
    """Lets the site be installed on a phone's home screen."""
    data = {
        "name": settings.SITE_NAME,
        "short_name": settings.SITE_NAME,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#faf6ef",
        "theme_color": "#b5452b",
        "icons": [
            {"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "share_target": {
            "action": reverse("meals:recipe_add"),
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
    }
    return JsonResponse(data, content_type="application/manifest+json")


# Pass-through service worker: makes the app installable without caching
# anything, so the data shown is always current.
SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


@cache_control(no_cache=True)
def service_worker(request):
    return HttpResponse(SERVICE_WORKER, content_type="application/javascript")
