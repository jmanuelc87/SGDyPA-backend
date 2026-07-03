import uuid

from django.db import models
from django.utils import timezone

from apps.platform.tenancy import NO_ORGANIZATION


class IdempotencyRecord(models.Model):
    # Client-supplied Idempotency-Key. Scoped to organization_id rather than
    # globally unique so one tenant cannot present another tenant's key and
    # replay its stored response. Requests without a tenant context (tenant-
    # agnostic endpoints) share the nil-UUID sentinel scope.
    key = models.UUIDField(editable=False)
    organization_id = models.UUIDField(editable=False)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    status_code = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "key"],
                name="uniq_idempotency_org_key",
            )
        ]


class AsyncJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        STARTED = "started", "Iniciada"
        COMPLETED = "completed", "Completada"
        FAILED = "failed", "Fallida"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_id = models.CharField(max_length=255, blank=True)
    operation = models.CharField(max_length=120)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # Owning tenant. Scopes the idempotency key so one organization cannot reuse
    # another's key and be handed that tenant's job. Tenant-agnostic requests use
    # the NO_ORGANIZATION sentinel.
    organization_id = models.UUIDField(default=NO_ORGANIZATION, editable=False)
    idempotency_key = models.CharField(max_length=255)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "idempotency_key"],
                name="uniq_asyncjob_org_idempotency_key",
            )
        ]

    def mark_started(self) -> None:
        self.status = self.Status.STARTED
        self.save(update_fields=["status", "updated_at"])

    def mark_completed(self, result: dict[str, object] | None = None) -> None:
        self.status = self.Status.COMPLETED
        self.result = result or {}
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "result", "completed_at", "updated_at"])

    def mark_failed(self, error: str) -> None:
        self.status = self.Status.FAILED
        self.error = error
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "error", "completed_at", "updated_at"])
