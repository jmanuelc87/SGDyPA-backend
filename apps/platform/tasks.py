from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from apps.platform.models import AsyncJob
from celery import shared_task  # type: ignore[import-untyped]


def idempotent_task[F: Callable[..., Any]](func: F) -> F:
    """Document the SGDyPA task convention and require an idempotency key."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not kwargs.get("idempotency_key"):
            raise ValueError("SGDyPA Celery tasks require idempotency_key")
        return func(*args, **kwargs)

    return cast(F, wrapper)


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    autoretry_for=(TimeoutError,),
    retry_backoff=True,
    max_retries=3,
)
@idempotent_task
def complete_async_job(
    self: object,
    *,
    job_id: str,
    idempotency_key: str,
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    job = AsyncJob.objects.get(pk=job_id, idempotency_key=idempotency_key)
    job.mark_started()
    payload = result or {"message": "job completed"}
    job.mark_completed(payload)
    return {"job_id": job_id, "status": job.status, "result": payload}
