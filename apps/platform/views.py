import json
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.urls import reverse
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.platform.models import AsyncJob
from apps.platform.tasks import complete_async_job


class HealthCheckView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        checked_at = timezone.now().replace(microsecond=0)
        return Response(
            {
                "status": "ok",
                "checked_at": checked_at,
            }
        )


def _job_payload(request: HttpRequest, job: AsyncJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "operation": job.operation,
        "status": job.status,
        "task_id": job.task_id,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": job.updated_at.isoformat().replace("+00:00", "Z"),
        "completed_at": (
            job.completed_at.isoformat().replace("+00:00", "Z")
            if job.completed_at
            else None
        ),
        "url": request.build_absolute_uri(
            reverse("platform:async-job-detail", kwargs={"job_id": job.id})
        ),
    }


def async_job_create(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": {"code": "method_not_allowed"}}, status=405)

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        return JsonResponse(
            {"error": {"code": "idempotency_key_required"}},
            status=400,
        )

    body = json.loads(request.body or b"{}")
    operation = str(body.get("operation") or "platform.noop")
    job, created = AsyncJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={"operation": operation},
    )

    if created:
        async_result = complete_async_job.apply_async(
            kwargs={
                "job_id": str(job.id),
                "idempotency_key": idempotency_key,
                "result": {"operation": operation},
            }
        )
        job.task_id = async_result.id or ""
        job.save(update_fields=["task_id", "updated_at"])
        job.refresh_from_db()

    response = _job_payload(request, job)
    return JsonResponse(response, status=202)


def async_job_detail(request: HttpRequest, job_id: str) -> JsonResponse:
    if request.method != "GET":
        return JsonResponse({"error": {"code": "method_not_allowed"}}, status=405)

    job = AsyncJob.objects.get(pk=job_id)
    return JsonResponse(_job_payload(request, job))
