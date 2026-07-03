/**
 * This file was generated from api/v1/openapi.yaml.
 * Do not edit manually; run `npm run generate:api-types` after changing the contract.
 */

export interface paths {
  "/health-checks": {
    get: operations["getHealthCheck"];
  };
  "/platform/async-jobs": {
    post: operations["createAsyncJob"];
  };
  "/platform/async-jobs/{job_id}": {
    get: operations["getAsyncJob"];
  };
  "/webhooks/documenso": {
    post: operations["receiveDocumensoWebhook"];
  };
}

export interface operations {
  getHealthCheck: {
    responses: {
      200: { content: { "application/json": components["schemas"]["HealthCheck"] } };
      default: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
    };
  };
  createAsyncJob: {
    parameters: {
      header: Pick<components["parameters"], "X-Organization-Id" | "Idempotency-Key">;
    };
    requestBody?: { content: { "application/json": components["schemas"]["AsyncJobCreateRequest"] } };
    responses: {
      202: { content: { "application/json": components["schemas"]["AsyncJob"] } };
      400: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      401: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      403: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      409: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      default: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
    };
  };
  getAsyncJob: {
    parameters: {
      header: Pick<components["parameters"], "X-Organization-Id">;
      path: { job_id: string };
    };
    responses: {
      200: { content: { "application/json": components["schemas"]["AsyncJob"] } };
      401: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      403: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      404: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      default: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
    };
  };
  receiveDocumensoWebhook: {
    parameters: {
      header: Pick<components["parameters"], "X-Webhook-Signature">;
    };
    requestBody: { content: { "application/json": Record<string, unknown> } };
    responses: {
      202: never;
      204: never;
      401: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
      default: { content: { "application/json": components["schemas"]["ErrorEnvelope"] } };
    };
  };
}

export interface components {
  schemas: {
    HealthCheck: {
      status: "ok";
      checked_at: string;
    };
    AsyncJobStatus: "pending" | "running" | "completed" | "failed";
    AsyncJobCreateRequest: {
      operation?: string;
    };
    AsyncJob: {
      id: string;
      operation: string;
      status: components["schemas"]["AsyncJobStatus"];
      task_id: string | null;
      result: Record<string, unknown> | null;
      error: Record<string, unknown> | null;
      created_at: string;
      updated_at: string;
      completed_at: string | null;
      url: string;
    };
    ErrorCode:
      | "scope_frozen"
      | "illegal_transition"
      | "legal_hold_active"
      | "self_approval_forbidden"
      | "validation_failed"
      | "stale_state"
      | "idempotency_key_required"
      | "idempotency_key_invalid"
      | "idempotency_key_conflict"
      | "authentication_failed"
      | "not_authenticated"
      | "not_found"
      | "method_not_allowed"
      | "permission_denied"
      | "parse_error"
      | "throttled"
      | "unsupported_media_type"
      | "internal_error";
    ErrorDetail: {
      field?: string;
      code?: components["schemas"]["ErrorCode"];
      message?: string;
      [key: string]: unknown;
    };
    ErrorEnvelope: {
      error: {
        code: components["schemas"]["ErrorCode"];
        message: string;
        details: components["schemas"]["ErrorDetail"][];
      };
    };
  };
  parameters: {
    "X-Organization-Id": string;
    "Idempotency-Key": string;
    "X-Webhook-Signature": string;
  };
}
