from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    keycloak_sub = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="Immutable Keycloak OIDC subject used as the identity anchor.",
    )
