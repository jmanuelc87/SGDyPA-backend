from enum import StrEnum


class ErrorCode(StrEnum):
    SCOPE_FROZEN = "scope_frozen"
    ILLEGAL_TRANSITION = "illegal_transition"
    LEGAL_HOLD_ACTIVE = "legal_hold_active"
    SELF_APPROVAL_FORBIDDEN = "self_approval_forbidden"
    VALIDATION_FAILED = "validation_failed"
    STALE_STATE = "stale_state"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_KEY_INVALID = "idempotency_key_invalid"
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"

    AUTHENTICATION_FAILED = "authentication_failed"
    NOT_AUTHENTICATED = "not_authenticated"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PERMISSION_DENIED = "permission_denied"
    PARSE_ERROR = "parse_error"
    THROTTLED = "throttled"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INTERNAL_ERROR = "internal_error"
