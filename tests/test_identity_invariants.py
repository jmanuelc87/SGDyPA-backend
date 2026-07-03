from __future__ import annotations

import pytest
from apps.findings_capa.models import FindingAssignment
from apps.trail.models import TrailEntry
from django.apps import apps
from django.db import models

DOMAIN_APP_LABELS = {
    "audit_process",
    "documents",
    "findings_capa",
    "identity",
    "platform",
    "rag",
    "retention_disposition",
    "trail",
}


@pytest.mark.django_db
def test_domain_relations_never_join_by_email() -> None:
    offenders: list[str] = []

    for model in apps.get_models():
        if model._meta.app_label not in DOMAIN_APP_LABELS:
            continue

        for field in model._meta.get_fields(include_hidden=True):
            if not field.is_relation or field.auto_created:
                continue

            if isinstance(field, models.ForeignKey | models.OneToOneField):
                target_field = field.target_field
                if target_field.name == "email" or target_field.attname == "email":
                    offenders.append(
                        f"{model._meta.label}.{field.name} -> "
                        f"{field.remote_field.model._meta.label}.{target_field.name}"
                    )

            if isinstance(field, models.ManyToManyField):
                through = field.remote_field.through
                for through_field in through._meta.fields:
                    if not isinstance(through_field, models.ForeignKey):
                        continue
                    target_field = through_field.target_field
                    if target_field.name == "email" or target_field.attname == "email":
                        offenders.append(
                            f"{model._meta.label}.{field.name} through "
                            f"{through._meta.label}.{through_field.name} -> "
                            f"{through_field.remote_field.model._meta.label}.{target_field.name}"
                        )

    assert offenders == []


def test_identity_snapshots_exist_on_trail_and_assignments() -> None:
    assert isinstance(
        TrailEntry._meta.get_field("actor_email_snapshot"), models.EmailField
    )
    assert isinstance(
        TrailEntry._meta.get_field("actor_display_name_snapshot"),
        models.CharField,
    )
    assert isinstance(
        FindingAssignment._meta.get_field("email_snapshot"),
        models.EmailField,
    )
    assert isinstance(
        FindingAssignment._meta.get_field("display_name_snapshot"),
        models.CharField,
    )
