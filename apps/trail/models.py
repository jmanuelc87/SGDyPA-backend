from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models

from apps.identity.models import Organization


class TrailEntryQuerySet(models.QuerySet["TrailEntry"]):
    def update(self, **kwargs: Any) -> int:
        raise TrailEntry.AppendOnlyError(
            "TrailEntry is append-only; bulk updates are forbidden."
        )

    def delete(self) -> tuple[int, dict[str, int]]:
        raise TrailEntry.AppendOnlyError(
            "TrailEntry is append-only; bulk deletes are forbidden."
        )


class TrailEntry(models.Model):
    class AppendOnlyError(RuntimeError):
        pass

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

    objects = TrailEntryQuerySet.as_manager()

    class Meta:
        # No default ordering: it would force an ORDER BY onto every queryset
        # (including COUNT/EXISTS) against an ever-growing table. Callers order
        # explicitly by sequence; physical per-tenant grouping comes from the
        # LIST-by-organization partitioning (ADR-0008).
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "sequence"],
                name="uniq_trail_entry_organization_sequence",
            )
        ]
        # All indexes are organization-prefixed: on the LIST-by-organization
        # partitioned table they collapse to compact per-tenant indexes, so a scoped
        # audit read prunes to one partition and scans only that tenant's rows
        # instead of the whole ledger.
        indexes = [
            models.Index(
                fields=["organization", "target_entity", "target_id", "sequence"],
                name="trail_entry_org_target_idx",
            ),
            models.Index(
                fields=["organization", "actor", "sequence"],
                name="trail_entry_org_actor_idx",
            ),
            models.Index(
                fields=["organization", "created_at"],
                name="trail_entry_org_created_idx",
            ),
            models.Index(
                fields=["organization", "action", "sequence"],
                name="trail_entry_org_action_idx",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding and self.pk:
            raise self.AppendOnlyError(
                "TrailEntry is append-only; updates are forbidden."
            )
        super().save(*args, **kwargs)

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        raise self.AppendOnlyError("TrailEntry is append-only; deletes are forbidden.")


class LedgerHead(models.Model):
    """Materialized per-tenant chain tip; the ``SELECT … FOR UPDATE`` lock target (ADR-0008).

    The lock and the data needed to append (the tip) are the *same row*: locking this row
    serializes ledger appends for the tenant without holding a lock on unrelated writes to
    the ``Organization`` row. Advanced in place on every append — not append-only.
    """

    organization = models.OneToOneField(
        Organization,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="ledger_head",
    )
    ultima_secuencia = models.PositiveBigIntegerField(default=0)
    ultimo_hash = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
