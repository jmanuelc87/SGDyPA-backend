# Generated for AUR-6: tenant-scope AsyncJob idempotency.

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("platform", "0002_idempotencyrecord"),
    ]

    operations = [
        migrations.AddField(
            model_name="asyncjob",
            name="organization_id",
            field=models.UUIDField(default=uuid.UUID(int=0), editable=False),
        ),
        # Drop the global unique on idempotency_key; uniqueness is now scoped to
        # the owning organization via the constraint below.
        migrations.AlterField(
            model_name="asyncjob",
            name="idempotency_key",
            field=models.CharField(max_length=255),
        ),
        migrations.AddConstraint(
            model_name="asyncjob",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "idempotency_key"),
                name="uniq_asyncjob_org_idempotency_key",
            ),
        ),
    ]
