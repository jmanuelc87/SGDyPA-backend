import json

from apps.platform.models import AsyncJob
from django.test import Client, TestCase, override_settings


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class AsyncJobApiTests(TestCase):
    def test_deferred_operation_returns_202_and_get_polls_resource(self) -> None:
        client = Client()

        response = client.post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "documents.reindex"}),
            content_type="application/json",
            headers={"Idempotency-Key": "reindex-doc-version-1"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["operation"], "documents.reindex")
        self.assertEqual(payload["status"], AsyncJob.Status.COMPLETED)
        self.assertTrue(payload["url"].endswith(f"/api/v1/platform/async-jobs/{payload['id']}"))

        poll_response = client.get(payload["url"])
        self.assertEqual(poll_response.status_code, 200)
        self.assertEqual(poll_response.json()["id"], payload["id"])
        self.assertEqual(poll_response.json()["status"], AsyncJob.Status.COMPLETED)

    def test_idempotency_key_reuses_existing_job(self) -> None:
        client = Client()
        headers = {"Idempotency-Key": "stable-operation-key"}

        first = client.post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
            headers=headers,
        )
        second = client.post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
            headers=headers,
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(AsyncJob.objects.count(), 1)

    def test_idempotency_key_is_required(self) -> None:
        response = Client().post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")
