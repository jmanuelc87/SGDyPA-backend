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
        return (
            self.filter(status=Membership.Status.ACTIVE)
            .filter(organization__is_active=True)
            .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
        )

    def invited(self) -> models.QuerySet:
        return self.filter(status=Membership.Status.INVITED)


class Role(models.Model):
    class SystemRole(models.TextChoices):
        P1 = "P1", "Auditor Líder"
        P2 = "P2", "Auditor"
        P3 = "P3", "Auditado"
        P4 = "P4", "Gestor Documental"
        P5 = "P5", "Responsable de Programa"
        P6 = "P6", "Administrador del Tenant"
        P7 = "P7", "Auditor Externo"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=20,
        choices=SystemRole.choices,
        unique=True,
        help_text="Stable P1-P7 system role code.",
    )
    name = models.CharField(max_length=120)
    capabilities = models.JSONField(
        default=list,
        blank=True,
        help_text="Read-only system capability slugs enforced server-side.",
    )
    is_system = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


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
    roles = models.ManyToManyField(
        Role,
        through="MembershipRole",
        related_name="memberships",
        blank=True,
    )

    class Meta:
        ordering = ["organization_id", "user_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="uniq_membership_organization_user",
            )
        ]

    @property
    def is_active(self) -> bool:
        if self.status != self.Status.ACTIVE:
            return False
        if not self.organization.is_active:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    def __str__(self) -> str:
        return f"{self.user_id}@{self.organization_id}"


class MembershipRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        Membership,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="membership_assignments",
    )
    assigned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["membership_id", "role__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "role"],
                name="uniq_membership_role",
            )
        ]

    def __str__(self) -> str:
        return f"{self.membership_id}:{self.role.code}"
