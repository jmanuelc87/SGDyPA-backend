from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.identity.models import Organization


class Finding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="findings",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization_id", "title", "id"]

    def __str__(self) -> str:
        return str(self.title)


class FindingAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.ForeignKey(
        Finding,
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="finding_assignments",
    )
    email_snapshot = models.EmailField(
        help_text="Historical assignee email snapshot (ADR-0002).",
    )
    display_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        help_text="Assignee display name snapshot (ADR-0002).",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["finding_id", "assigned_at", "id"]
