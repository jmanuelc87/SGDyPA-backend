from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.identity.models import Organization


class RetentionClass(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="retention_classes",
    )
    nombre = models.CharField(max_length=120)
    periodo_retencion_meses = models.PositiveIntegerField()
    worm_habilitado = models.BooleanField(default=True)
    es_sensible = models.BooleanField(default=False)
    aprobadores_disposicion_requeridos = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization_id", "nombre", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "nombre"],
                name="uniq_retention_class_org_nombre",
            ),
            models.CheckConstraint(
                condition=models.Q(aprobadores_disposicion_requeridos__gte=1),
                name="retention_class_aprobadores_gte_1",
            ),
        ]

    def __str__(self) -> str:
        return str(self.nombre)


class DocumentType(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="document_types",
    )
    retention_class = models.ForeignKey(
        RetentionClass,
        on_delete=models.PROTECT,
        related_name="document_types",
    )
    nombre = models.CharField(max_length=120)
    descripcion = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization_id", "nombre", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "nombre"],
                name="uniq_document_type_org_nombre",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.retention_class_id
            and self.organization_id
            and self.retention_class.organization_id != self.organization_id
        ):
            raise ValidationError(
                {
                    "retention_class": (
                        "La clase de retención debe pertenecer a la misma organización "
                        "que el tipo documental."
                    )
                }
            )

    def __str__(self) -> str:
        return str(self.nombre)
