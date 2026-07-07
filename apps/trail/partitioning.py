"""Shared helpers for LIST-by-organization partitioning of ``trail_trailentry`` (ADR-0008).

One partition per tenant keeps each organization's audit rows physically isolated, so a
scoped read prunes to a single partition. These helpers are the single source of truth for
partition names and the create/detach DDL, reused by the partition migration, the
``Organization`` ``post_save`` signal, and the ``ensure_trail_partitions`` /
``archive_trail_partition`` management commands.

Everything here is Postgres-only. Callers on other backends (sqlite in tests/dev) must guard
on ``connection.vendor == "postgresql"`` — the flat table has no partitions.
"""

from __future__ import annotations

from uuid import UUID

from django.db import connection

#: Parent partitioned table and its DEFAULT catch-all partition.
PARENT_TABLE = "trail_trailentry"
DEFAULT_PARTITION = "trail_trailentry_default"


def _normalize(organization_id: UUID | str) -> UUID:
    return (
        organization_id
        if isinstance(organization_id, UUID)
        else UUID(str(organization_id))
    )


def partition_name_for_organization(organization_id: UUID | str) -> str:
    """Return the deterministic partition table name for one organization.

    Uses the UUID hex (no hyphens) so the identifier is a valid, stable Postgres name well
    under the 63-char limit: ``trail_trailentry_<32 hex>`` = 49 chars.
    """

    return f"{PARENT_TABLE}_{_normalize(organization_id).hex}"


def create_partition_for_organization(organization_id: UUID | str) -> str:
    """Create this tenant's partition if absent. Returns the partition name.

    Idempotent (``IF NOT EXISTS``) and safe to call from the org ``post_save`` signal or the
    reconcile command. Invoke only on Postgres.
    """

    name = partition_name_for_organization(organization_id)
    with connection.cursor() as cursor:
        cursor.execute(
            f'CREATE TABLE IF NOT EXISTS "{name}" '
            f"PARTITION OF {PARENT_TABLE} FOR VALUES IN (%s)",
            [str(_normalize(organization_id))],
        )
    return name


def is_partitioned() -> bool:
    """True when ``trail_trailentry`` is a partitioned table on the current (Postgres) DB.

    Lets provisioning paths short-circuit cleanly when the migration has not (or cannot, on
    sqlite) partition the table yet.
    """

    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relkind = 'p' FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s AND n.nspname = current_schema()",
            [PARENT_TABLE],
        )
        row = cursor.fetchone()
    return bool(row and row[0])
