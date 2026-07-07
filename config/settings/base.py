import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]

# Load environment variables from a local .env file (if present) before any
# os.environ lookups below. Real environment variables always take precedence.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = "change-me-in-environment"
DEBUG = False
ALLOWED_HOSTS: list[str] = []

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

BOUNDED_CONTEXT_APPS = [
    "apps.identity",
    "apps.documents",
    "apps.retention_disposition",
    "apps.audit_process",
    "apps.findings_capa",
    "apps.trail",
    "apps.rag",
    "apps.platform",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "rest_framework",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + BOUNDED_CONTEXT_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # CorsMiddleware must run before CommonMiddleware (and any view that can
    # generate a response) so CORS headers are attached to every response,
    # including preflight OPTIONS requests.
    "corsheaders.middleware.CorsMiddleware",
    "apps.platform.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.identity.middleware.KeycloakBearerAuthenticationMiddleware",
    "apps.platform.middleware.TenantContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite3")

if DB_ENGINE in ("postgres", "postgresql", "django.db.backends.postgresql"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "sgdypa"),
            "USER": os.environ.get("POSTGRES_USER", "sgdypa"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "sgdypa_dev_password"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
            "OPTIONS": {
                "sslmode": os.environ.get("POSTGRES_SSLMODE", "prefer"),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

LANGUAGE_CODE = "es-mx"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "identity.User"

KEYCLOAK_OIDC = {
    "ISSUER": os.environ.get("KEYCLOAK_OIDC_ISSUER"),
    "AUDIENCE": os.environ.get("KEYCLOAK_OIDC_AUDIENCE"),
    "JWKS_URL": os.environ.get("KEYCLOAK_OIDC_JWKS_URL"),
    "ALGORITHMS": tuple(
        algorithm.strip()
        for algorithm in os.environ.get("KEYCLOAK_OIDC_ALGORITHMS", "RS256").split(",")
        if algorithm.strip()
    ),
}

# Keycloak -> backend replication webhook. Keycloak admin events are POSTed to
# /api/v1/identity/keycloak/events and authenticated by an HMAC-SHA256 signature
# over the raw body (NOT a JWT). When SECRET is unset the endpoint is disabled
# and fails closed with 503.
KEYCLOAK_WEBHOOK = {
    "SECRET": os.environ.get("KEYCLOAK_WEBHOOK_SECRET"),
    "SIGNATURE_HEADER": os.environ.get(
        "KEYCLOAK_WEBHOOK_SIGNATURE_HEADER", "X-Keycloak-Signature"
    ),
}

# Keycloak group -> local Organization membership replication. When ENABLED, a
# user's Keycloak group membership drives local Membership rows and their mapped
# realm/client roles drive P1-P7 MembershipRole rows, reconciled at login (full
# token, authoritative) and via GROUP_MEMBERSHIP admin events (incremental).
# Fails safe: OFF by default, so no membership is ever created or pruned unless
# an operator opts in.
#
# Keycloak setup required when ENABLED: a Group Membership token mapper with
# "Full group path" on (claim name = GROUPS_CLAIM, added to the access token),
# the realm-roles scope on the access token, client-role mappers if used, and
# the admin-event webhook SPI delivering GROUP_MEMBERSHIP events (not only USER).
#
# ROLE_MAP maps Keycloak role names -> local P1-P7 codes. Roles are GLOBAL: the
# mapped codes apply to every group-derived membership the user has.
DEFAULT_ROLE_MAP: dict[str, str] = {}

KEYCLOAK_ORG_REPLICATION = {
    "ENABLED": os.environ.get("KEYCLOAK_ORG_REPLICATION_ENABLED", "false").lower()
    == "true",
    "GROUPS_CLAIM": os.environ.get("KEYCLOAK_GROUPS_CLAIM", "groups"),
    "ROLE_MAP": json.loads(os.environ.get("KEYCLOAK_ROLE_MAP", "{}"))
    or DEFAULT_ROLE_MAP,
}

# Cross-Origin Resource Sharing. The SPA (e.g. the Vite dev server) is served
# from a different origin than this API, so browsers require the API to opt the
# origin in explicitly. Auth is stateless bearer tokens in the Authorization
# header (no cookies), so credentials are not enabled. Origins come from the
# CORS_ALLOWED_ORIGINS env var as a comma-separated list; dev supplies a default.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

REST_FRAMEWORK = {
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%SZ",
    "EXCEPTION_HANDLER": "apps.platform.api_errors.api_exception_handler",
    # Stateless bearer auth: the Keycloak middleware validates the token and this
    # authenticator surfaces the result to DRF. Replacing DRF's SessionAuthentication
    # default removes CSRF enforcement, which does not apply to token auth.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.identity.authentication.KeycloakBearerDRFAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TRACK_STARTED = True
CELERY_BEAT_SCHEDULE: dict[str, dict[str, object]] = {}
