import uuid

from django.db import models
from django.utils import timezone


class IdempotencyRecord(models.Model):
    key = models.UUIDField(primary_key=True, editable=False)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    status_code = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


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
    idempotency_key = models.CharField(max_length=255, unique=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

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
