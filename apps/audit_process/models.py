from __future__ import annotations

import uuid

from django.db import models

from apps.identity.models import Organization


class AuditProcess(models.Model):
    """Authoritative audit process state held by the backend FSM."""

    class Tipo(models.TextChoices):
        INTERNA = "INTERNA", "Interna"
        SEGUNDA_PARTE = "SEGUNDA_PARTE", "Segunda parte"
        TERCERA_PARTE = "TERCERA_PARTE", "Tercera parte"

    class Estado(models.TextChoices):
        PLANIFICADA = "PLANIFICADA", "Planificada"
        EN_EJECUCION = "EN_EJECUCION", "En ejecución"
        EN_CIERRE = "EN_CIERRE", "En cierre"
        INFORME_EMITIDO = "INFORME_EMITIDO", "Informe emitido"
        EN_SEGUIMIENTO = "EN_SEGUIMIENTO", "En seguimiento"
        CERRADA = "CERRADA", "Cerrada"
        CANCELADA = "CANCELADA", "Cancelada"
        POSTERGADA = "POSTERGADA", "Postergada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="audit_processes",
    )
    nombre = models.CharField(max_length=255)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    estado = models.CharField(
        max_length=20,
        choices=Estado.choices,
        default=Estado.PLANIFICADA,
    )
    estado_pre_postergacion = models.CharField(
        max_length=20,
        choices=Estado.choices,
        blank=True,
        help_text=(
            "Estado desde el que se postergó; permite reanudar sin crear "
            "flujos por tipo."
        ),
    )
    motivo_excepcion = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["organization", "estado"]),
            models.Index(fields=["organization", "tipo"]),
        ]

    def __str__(self) -> str:
        return f"{self.nombre} · {self.get_estado_display()}"
