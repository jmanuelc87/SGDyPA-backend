from __future__ import annotations

import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.identity.models import Organization
from apps.retention_disposition.models import DocumentType


class Document(models.Model):
    class EstadoCicloVida(models.TextChoices):
        BORRADOR = "borrador", "Borrador"
        EN_REVISION = "en_revision", "En revisión"
        PUBLICADO = "publicado", "Publicado"
        RETENIDO = "retenido", "Retenido"
        DISPUESTO = "dispuesto", "Dispuesto"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="documents",
    )
    document_type = models.ForeignKey(
        DocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
    )
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_documents",
    )
    titulo = models.CharField(max_length=255)
    estado_ciclo_vida = models.CharField(
        max_length=20,
        choices=EstadoCicloVida.choices,
        default=EstadoCicloVida.BORRADOR,
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization_id", "titulo", "id"]
        indexes = [
            models.Index(
                fields=["organization", "estado_ciclo_vida"],
                name="document_org_estado_idx",
            ),
            models.Index(
                fields=["organization", "document_type"],
                name="document_org_type_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.document_type_id
            and self.organization_id
            and self.document_type.organization_id != self.organization_id
        ):
            raise ValidationError(
                {
                    "document_type": (
                        "El tipo documental debe pertenecer a la misma organización "
                        "que el documento."
                    )
                }
            )

    def __str__(self) -> str:
        return str(self.titulo)


class DocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_document_versions",
    )
    numero_version = models.PositiveIntegerField()
    hash_contenido = models.CharField(max_length=64, editable=False)
    uri_almacenamiento = models.CharField(max_length=1024)
    es_firmada = models.BooleanField(default=False)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id", "numero_version", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "numero_version"],
                name="uniq_document_version_number",
            ),
            models.UniqueConstraint(
                fields=["document", "hash_contenido"],
                name="uniq_document_version_hash",
            ),
            models.CheckConstraint(
                condition=models.Q(numero_version__gte=1),
                name="document_version_number_gte_1",
            ),
        ]
        indexes = [
            models.Index(fields=["hash_contenido"], name="document_version_hash_idx")
        ]

    @staticmethod
    def calculate_content_hash(content: bytes | str) -> str:
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def set_content_hash(self, content: bytes | str) -> None:
        self.hash_contenido = self.calculate_content_hash(content)

    def __str__(self) -> str:
        return f"{self.document_id} v{self.numero_version}"
