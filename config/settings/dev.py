from .base import *  # noqa: F403

DEBUG = True
# "host.docker.internal" lets the Keycloak container reach the host-run app for
# the admin-event replication webhook (see docker/keycloak/spi).
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "host.docker.internal"]

# Default the SPA dev-server origins (Vite on :5173) when the env var is unset,
# so local frontend development works out of the box.
if not CORS_ALLOWED_ORIGINS:  # noqa: F405
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
