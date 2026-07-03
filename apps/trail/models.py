from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.identity.models import Organization


class TrailEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="trail_entries",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="trail_entries",
    )
    actor_email_snapshot = models.EmailField(
        help_text="Historical actor email snapshot (ADR-0002).",
    )
    actor_display_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        help_text="Historical actor display name snapshot (ADR-0002).",
    )
    action = models.CharField(max_length=120)
    target_entity = models.CharField(max_length=120)
    target_id = models.UUIDField()
    payload = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveBigIntegerField()
    previous_hash = models.CharField(max_length=64, blank=True)
    entry_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["organization_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "sequence"],
                name="uniq_trail_entry_organization_sequence",
            )
        ]
