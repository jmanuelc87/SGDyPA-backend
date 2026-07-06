from .base import *  # noqa: F403

DEBUG = True
# "host.docker.internal" lets the Keycloak container reach the host-run app for
# the admin-event replication webhook (see docker/keycloak/spi).
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "host.docker.internal"]
