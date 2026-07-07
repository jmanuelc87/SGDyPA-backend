"""Convert ``trail_trailentry`` into a LIST-by-organization partitioned table (ADR-0008).

Postgres only. Each tenant gets its own partition, so a per-org audit read prunes to a
single partition and scans only that tenant's rows. ``organization_id`` is the partition
key and already leads ``UNIQUE(organization, sequence)``, so the dense-sequence backstop is
preserved unchanged.

This is expressed as ``SeparateDatabaseAndState`` with **no** state operations: the ORM
keeps believing it is the flat table created in ``0001``/``0003`` (identical columns,
constraints, and indexes), and partitioning stays invisible to ``makemigrations``. A plain
table cannot be altered into a partitioned one in place, so the database side does the
standard create-partitioned / copy / swap. On sqlite the whole thing is a no-op (the flat
table stays).

New tenants get their partition from the ``Organization`` ``post_save`` signal
(``apps/trail/signals.py``); ``manage.py ensure_trail_partitions`` reconciles any gaps; the
``DEFAULT`` partition guarantees appends never fail even before a tenant partition exists.
"""

from __future__ import annotations

from django.db import migrations

from apps.trail.partitioning import partition_name_for_organization


def _add_pk_unique_indexes(execute):
    """(Re)create the composite PK, preserved unique constraint, and four read indexes.

    Done *after* the old table is dropped so the canonical index/constraint names (which
    are schema-global in Postgres) are free. On a partitioned parent these cascade to every
    partition automatically.
    """

    execute(
        "ALTER TABLE trail_trailentry "
        "ADD CONSTRAINT trail_trailentry_pkey PRIMARY KEY (organization_id, id)"
    )
    execute(
        "ALTER TABLE trail_trailentry "
        "ADD CONSTRAINT uniq_trail_entry_organization_sequence "
        "UNIQUE (organization_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_target_idx ON trail_trailentry "
        "(organization_id, target_entity, target_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_actor_idx ON trail_trailentry "
        "(organization_id, actor_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_created_idx ON trail_trailentry "
        "(organization_id, created_at)"
    )
    execute(
        "CREATE INDEX trail_entry_org_action_idx ON trail_trailentry "
        "(organization_id, action, sequence)"
    )


def convert_to_partitioned(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    Organization = apps.get_model("identity", "Organization")
    execute = schema_editor.execute

    # 1. New partitioned parent, columns/defaults/storage cloned from the flat table but
    #    WITHOUT its indexes/constraints — those names stay held by the flat table until
    #    step 4.
    execute(
        "CREATE TABLE trail_trailentry_part "
        "(LIKE trail_trailentry INCLUDING DEFAULTS INCLUDING STORAGE) "
        "PARTITION BY LIST (organization_id)"
    )
    # 2. DEFAULT safety partition, then one partition per pre-existing tenant.
    execute(
        "CREATE TABLE trail_trailentry_default "
        "PARTITION OF trail_trailentry_part DEFAULT"
    )
    for organization_id in Organization.objects.values_list("id", flat=True):
        execute(
            f'CREATE TABLE "{partition_name_for_organization(organization_id)}" '
            "PARTITION OF trail_trailentry_part FOR VALUES IN (%s)",
            [str(organization_id)],
        )
    # 3. Route existing rows into their tenant partitions.
    execute("INSERT INTO trail_trailentry_part SELECT * FROM trail_trailentry")
    # 4. Swap the partitioned table in for the flat one (frees the canonical names).
    execute("DROP TABLE trail_trailentry")
    execute("ALTER TABLE trail_trailentry_part RENAME TO trail_trailentry")
    # 5. Now that the names are free, add PK/unique/indexes with their canonical names.
    _add_pk_unique_indexes(execute)


def revert_to_flat(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    execute = schema_editor.execute
    # Mirror of the forward path: plain table (no indexes), copy rows back, drop the
    # partitioned tree to free the names, rename, then recreate PK/unique/indexes canonically.
    execute(
        "CREATE TABLE trail_trailentry_flat "
        "(LIKE trail_trailentry INCLUDING DEFAULTS INCLUDING STORAGE)"
    )
    execute("INSERT INTO trail_trailentry_flat SELECT * FROM trail_trailentry")
    execute("DROP TABLE trail_trailentry")  # drops every partition with it
    execute("ALTER TABLE trail_trailentry_flat RENAME TO trail_trailentry")
    execute(
        "ALTER TABLE trail_trailentry "
        "ADD CONSTRAINT trail_trailentry_pkey PRIMARY KEY (id)"
    )
    execute(
        "ALTER TABLE trail_trailentry "
        "ADD CONSTRAINT uniq_trail_entry_organization_sequence "
        "UNIQUE (organization_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_target_idx ON trail_trailentry "
        "(organization_id, target_entity, target_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_actor_idx ON trail_trailentry "
        "(organization_id, actor_id, sequence)"
    )
    execute(
        "CREATE INDEX trail_entry_org_created_idx ON trail_trailentry "
        "(organization_id, created_at)"
    )
    execute(
        "CREATE INDEX trail_entry_org_action_idx ON trail_trailentry "
        "(organization_id, action, sequence)"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("trail", "0003_trailentry_indexes"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunPython(convert_to_partitioned, revert_to_flat),
            ],
        ),
    ]
