import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = "change-me-in-environment"
DEBUG = False
ALLOWED_HOSTS: list[str] = []

DJANGO_APPS = [
    "django.contrib.admin",
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

INSTALLED_APPS = DJANGO_APPS + BOUNDED_CONTEXT_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.identity.middleware.KeycloakBearerAuthenticationMiddleware",
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
