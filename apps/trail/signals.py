"""Provision trail tenant lock rows and partitions when organizations are created.

The LIST-by-organization scheme needs one partition per tenant. This post_save
receiver creates the tenant's LEDGER_HEAD row immediately and, on PostgreSQL,
creates the tenant partition after commit. Bulk imports are still covered by the
DEFAULT partition plus ``manage.py ensure_trail_partitions``.
"""

from __future__ import annotations

from typing import Any

from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.identity.models import Organization
from apps.trail.models import LedgerHead
from apps.trail.partitioning import create_partition_for_organization, is_partitioned


@receiver(  # type: ignore[untyped-decorator]
    post_save, sender=Organization, dispatch_uid="trail_create_org_partition"
)
def create_trail_partition_for_organization(
    sender: type[Organization], instance: Organization, created: bool, **kwargs: Any
) -> None:
    if not created:
        return

    # ADR-0008 requires a stable per-tenant lock target before append paths run.
    # Creating the head in the organization transaction lets append_trail_entry
    # start with a locked read of LEDGER_HEAD instead of racing to create it.
    LedgerHead.objects.get_or_create(
        organization=instance, defaults={"ultima_secuencia": 0, "ultimo_hash": ""}
    )

    if connection.vendor != "postgresql" or not is_partitioned():
        return

    # Defer until the surrounding transaction commits: the partition DDL takes a
    # brief lock on the parent table, and there is no need to hold it inside the
    # org-creation transaction.
    transaction.on_commit(lambda: create_partition_for_organization(instance.pk))
