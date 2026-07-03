from __future__ import annotations

import uuid
from typing import cast
from uuid import UUID

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return str(self.name)


class User(AbstractUser):
    keycloak_sub = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="Immutable Keycloak OIDC subject used as the identity anchor.",
    )
    email_verified = models.BooleanField(
        default=False,
        help_text="Projected from Keycloak; never used as an identity join key.",
    )
    display_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Projected display name synchronized from Keycloak claims.",
    )

    def has_organization_membership(self, organization_id: UUID) -> bool:
        return cast(
            bool,
            self.organization_memberships.active()
            .filter(organization_id=organization_id)
            .exists(),
        )


class MembershipQuerySet(models.QuerySet):
    def active(self) -> models.QuerySet:
        now = timezone.now()
        return self.filter(status=Membership.Status.ACTIVE).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
        )

    def invited(self) -> models.QuerySet:
        return self.filter(status=Membership.Status.INVITED)


class Membership(models.Model):
    class Status(models.TextChoices):
        INVITED = "invited", "Invitada"
        ACTIVE = "active", "Activa"
        SUSPENDED = "suspended", "Suspendida"
        REVOKED = "revoked", "Revocada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="organization_memberships",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INVITED,
    )
    scope = models.JSONField(
        default=dict,
        blank=True,
        help_text="Read-only third-party invitation scope (alcance).",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional invitation/access expiration timestamp (expira_en).",
    )
    invited_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MembershipQuerySet.as_manager()

    class Meta:
        ordering = ["organization_id", "user_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="uniq_membership_organization_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.organization_id}"
