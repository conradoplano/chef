"""
Django settings for Chef, the family meal planner.

All deployment-specific values come from environment variables so the same
image can run locally and on the NAS.
"""
import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Persistent data (SQLite database) lives here; mount it as a volume in Docker.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable must be set when DEBUG is off.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# Set when running behind a reverse proxy that terminates TLS (e.g. Synology / nginx).
if env_bool("BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "core",
    "meals",
    "connect",
]

SITE_NAME = os.environ.get("SITE_NAME", "Chef")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "meals.middleware.HouseholdMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.site",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        # Menu planning writes from a background thread while phones poll the shopping list:
        # WAL lets reads and a write happen together, and writers wait instead of failing.
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
    }
}

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "meals:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Keep family members logged in for a long time (default 90 days).
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", 60 * 60 * 24 * 90))
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

# How long an emailed login code stays valid, in seconds.
LOGIN_CODE_MAX_AGE = int(os.environ.get("LOGIN_CODE_MAX_AGE", 10 * 60))

# Local testing only: log in by entering a known email, without a code or email.
# Never enable this on the NAS.
DEV_LOGIN = env_bool("DEV_LOGIN", False)
if DEV_LOGIN:
    import sys

    print("WARNING: DEV_LOGIN is on - anyone can log in with just an email address.", file=sys.stderr)

LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en-us")
TIME_ZONE = os.environ.get("TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # The hashed manifest only exists after collectstatic (done in the Docker build).
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Email: SMTP when EMAIL_HOST is set, otherwise login links are printed to the
# console / container logs (handy for local dev or a first NAS setup).
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "chef@localhost")

# Uploaded recipe photos, next to the database (back up the whole data folder).
# They're served by a login-protected view, never as public files.
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"
RECIPE_PHOTO_MAX_MB = 25  # per photo, before it's scaled down
RECIPE_PHOTOS_PER_IMPORT = 6

# AI menu planning (OpenAI). Without an API key the "Create menu" feature is switched off.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-5.4-mini")
AI_EFFORT = os.environ.get("AI_EFFORT", "medium")
# What households may spend on AI per day (USD): each one (admins can set another limit per
# household), and all of them together.
AI_DAILY_LIMIT_USD = Decimal(os.environ.get("AI_DAILY_LIMIT_USD", "0.20"))
AI_GLOBAL_DAILY_LIMIT_USD = Decimal(os.environ.get("AI_GLOBAL_DAILY_LIMIT_USD", "2.00"))
# Anyone can register a household (AI after an admin approves it). Set to false to invite only.
REGISTRATION_OPEN = env_bool("REGISTRATION_OPEN", True)
# Email the household (including whoever asked) when a menu is created or changed (uses the SMTP settings above).
MENU_EMAILS = env_bool("MENU_EMAILS", True)
# Address of the app, for links in emails; defaults to the first CSRF_TRUSTED_ORIGINS entry.
SITE_URL = os.environ.get("SITE_URL", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
