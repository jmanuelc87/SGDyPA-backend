import json

from apps.platform.models import AsyncJob, IdempotencyRecord
from django.test import Client, TestCase, override_settings


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class AsyncJobApiTests(TestCase):
    def test_deferred_operation_returns_202_and_get_polls_resource(self) -> None:
        client = Client()

        # The task is dispatched via transaction.on_commit, so it only runs once
        # the request transaction commits; capture and execute those callbacks to
        # exercise the deferred completion.
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = client.post(
                "/api/v1/platform/async-jobs",
                data=json.dumps({"operation": "documents.reindex"}),
                content_type="application/json",
                headers={"Idempotency-Key": "9d0e7f7f-99e8-4b07-a6a0-cab5727e2f0d"},
            )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["operation"], "documents.reindex")
            # The response is built before commit, so the job is still pending;
            # the client polls the returned URL for completion.
            self.assertEqual(payload["status"], AsyncJob.Status.PENDING)
            self.assertTrue(
                payload["url"].endswith(f"/api/v1/platform/async-jobs/{payload['id']}")
            )

        self.assertEqual(len(callbacks), 1)

        poll_response = client.get(payload["url"])
        self.assertEqual(poll_response.status_code, 200)
        self.assertEqual(poll_response.json()["id"], payload["id"])
        self.assertEqual(poll_response.json()["status"], AsyncJob.Status.COMPLETED)

    def test_idempotency_key_reuses_existing_job(self) -> None:
        client = Client()
        headers = {"Idempotency-Key": "1a7f71a8-50e4-48b2-b7f6-485a6965ab0d"}

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

    def test_idempotency_key_must_be_uuid(self) -> None:
        response = Client().post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
            headers={"Idempotency-Key": "not-a-uuid"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_invalid")

    def test_idempotency_replay_returns_same_response_without_reexecuting(self) -> None:
        client = Client()
        headers = {"Idempotency-Key": "19cd67d4-e658-4f18-9b42-d02ff6a037a9"}

        first = client.post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
            headers=headers,
        )
        second = client.post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "documents.reindex"}),
            content_type="application/json",
            headers=headers,
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(AsyncJob.objects.count(), 1)

    def test_idempotency_key_reused_across_endpoints_conflicts(self) -> None:
        # A key already recorded for a different request must not replay that
        # unrelated response when presented to another endpoint.
        key = "3f2c9b1e-7d4a-4c6b-8e21-0a5b6c7d8e9f"
        IdempotencyRecord.objects.create(
            key=key,
            # Nil-UUID scope matches the tenant-agnostic async-jobs request.
            organization_id="00000000-0000-0000-0000-000000000000",
            method="POST",
            path="/api/v1/platform/other",
            status_code=201,
            response_body={"resource": "other"},
        )

        response = Client().post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
            headers={"Idempotency-Key": key},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_conflict")
        self.assertEqual(AsyncJob.objects.count(), 0)

    def test_idempotency_key_is_scoped_per_organization(self) -> None:
        # A key stored under a different organization must not replay here: the
        # request runs normally under its own scope instead of leaking the other
        # tenant's stored response.
        key = "6b1e3f2c-4c6b-7d4a-8e21-0a5b6c7d8e9f"
        IdempotencyRecord.objects.create(
            key=key,
            organization_id="11111111-1111-1111-1111-111111111111",
            method="POST",
            path="/api/v1/platform/async-jobs",
            status_code=202,
            response_body={"leaked": "other-tenant-data"},
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = Client().post(
                "/api/v1/platform/async-jobs",
                data=json.dumps({"operation": "signature.start"}),
                content_type="application/json",
                headers={"Idempotency-Key": key},
            )

        self.assertEqual(response.status_code, 202)
        self.assertNotIn("leaked", response.json())
        self.assertEqual(AsyncJob.objects.count(), 1)
        # The same key now exists independently in each organization's scope.
        self.assertEqual(IdempotencyRecord.objects.filter(key=key).count(), 2)

    def test_async_job_not_shared_across_organizations(self) -> None:
        # A job owned by another organization under the same idempotency key must
        # not be handed back; a fresh job is created in the request's own scope.
        other_org = "22222222-2222-2222-2222-222222222222"
        shared_key = "7c3d0a4b-4c6b-8e21-4c6b-0a5b6c7d8e9f"
        other_job = AsyncJob.objects.create(
            organization_id=other_org,
            idempotency_key=shared_key,
            operation="documents.reindex",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = Client().post(
                "/api/v1/platform/async-jobs",
                data=json.dumps({"operation": "signature.start"}),
                content_type="application/json",
                headers={"Idempotency-Key": shared_key},
            )

        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(response.json()["id"], str(other_job.id))
        self.assertEqual(AsyncJob.objects.count(), 2)

    def test_idempotency_key_is_required(self) -> None:
        response = Client().post(
            "/api/v1/platform/async-jobs",
            data=json.dumps({"operation": "signature.start"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")
