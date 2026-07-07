"""Provision a ``trail_trailentry`` partition when a new organization is created (ADR-0008).

The LIST-by-organization scheme needs one partition per tenant. This ``post_save`` receiver
creates it as soon as the ``Organization`` row is committed, so the tenant's first audit
append lands in its own partition rather than the ``DEFAULT`` catch-all. It is a best-effort
fast path: idempotent, Postgres-only, and paths that bypass signals (bulk imports) are covered
by the ``DEFAULT`` partition plus ``manage.py ensure_trail_partitions``.
"""

from __future__ import annotations

from typing import Any

from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.identity.models import Organization
from apps.trail.partitioning import create_partition_for_organization, is_partitioned


@receiver(  # type: ignore[untyped-decorator]
    post_save, sender=Organization, dispatch_uid="trail_create_org_partition"
)
def create_trail_partition_for_organization(
    sender: type[Organization], instance: Organization, created: bool, **kwargs: Any
) -> None:
    if not created or connection.vendor != "postgresql" or not is_partitioned():
        return

    # Defer until the surrounding transaction commits: the partition DDL takes a brief lock on
    # the parent table, and there is no need to hold it inside the org-creation transaction.
    transaction.on_commit(lambda: create_partition_for_organization(instance.pk))
