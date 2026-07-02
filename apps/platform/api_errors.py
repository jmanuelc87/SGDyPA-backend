from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from django.http import HttpRequest, JsonResponse
from rest_framework import exceptions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.platform.error_codes import ErrorCode
from apps.platform.exceptions import StableAPIException

SENSITIVE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\btraceback\b",
        r"\b(stack trace|stacktrace)\b",
        r"\b(select|insert|update|delete|drop|alter|create)\b.+\b(from|into|table|where|values)\b",
        r"\b(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
    )
]

EXCEPTION_CODE_MAP: dict[type[Exception], ErrorCode] = {
    exceptions.AuthenticationFailed: ErrorCode.AUTHENTICATION_FAILED,
    exceptions.NotAuthenticated: ErrorCode.NOT_AUTHENTICATED,
    exceptions.NotFound: ErrorCode.NOT_FOUND,
    exceptions.MethodNotAllowed: ErrorCode.METHOD_NOT_ALLOWED,
    exceptions.PermissionDenied: ErrorCode.PERMISSION_DENIED,
    exceptions.ParseError: ErrorCode.PARSE_ERROR,
    exceptions.Throttled: ErrorCode.THROTTLED,
    exceptions.UnsupportedMediaType: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    response = drf_exception_handler(exc, context)
    request = context.get("request")

    if response is None:
        return build_error_response(
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error.",
            details=[],
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            request=request,
        )

    code = get_error_code(exc)
    message = get_error_message(exc, code)
    details = get_error_details(exc, code, response.data)

    return build_error_response(
        code=code,
        message=message,
        details=details,
        status_code=response.status_code,
        request=request,
    )


def build_error_response(
    *,
    code: ErrorCode,
    message: str,
    details: object,
    status_code: int,
    request: Request | HttpRequest | None,
) -> Response:
    return Response(
        build_error_payload(
            code=code,
            message=message,
            details=details,
            request=request,
        ),
        status=status_code,
    )


def build_error_json_response(
    *,
    code: ErrorCode,
    message: str,
    details: object,
    status_code: int,
    request: Request | HttpRequest | None,
) -> JsonResponse:
    return JsonResponse(
        build_error_payload(
            code=code,
            message=message,
            details=details,
            request=request,
        ),
        status=status_code,
    )


def build_error_payload(
    *,
    code: ErrorCode,
    message: str,
    details: object,
    request: Request | HttpRequest | None,
) -> dict[str, object]:
    return {
        "error": {
            "code": code.value,
            "message": sanitize_text(message, fallback="Internal server error."),
            "details": sanitize_details(details),
            "request_id": get_request_id(request),
        }
    }


def get_error_code(exc: Exception) -> ErrorCode:
    if isinstance(exc, StableAPIException):
        return exc.code
    if isinstance(exc, serializers.ValidationError):
        return ErrorCode.VALIDATION_FAILED
    for exception_type, code in EXCEPTION_CODE_MAP.items():
        if isinstance(exc, exception_type):
            return code
    return ErrorCode.INTERNAL_ERROR


def get_error_message(exc: Exception, code: ErrorCode) -> str:
    if isinstance(exc, StableAPIException):
        return str(exc.detail)
    if code == ErrorCode.VALIDATION_FAILED:
        return "Request validation failed."
    if isinstance(exc, exceptions.APIException):
        return str(exc.detail)
    return "Internal server error."


def get_error_details(exc: Exception, code: ErrorCode, data: object) -> object:
    if isinstance(exc, StableAPIException):
        return exc.details
    if code == ErrorCode.VALIDATION_FAILED:
        return data
    return []


def sanitize_details(details: object) -> object:
    if isinstance(details, str):
        return sanitize_text(details, fallback="Internal error.")
    if isinstance(details, Mapping):
        return {
            str(key): sanitize_details(value)
            for key, value in details.items()
            if str(key) != "traceback"
        }
    if isinstance(details, Sequence) and not isinstance(details, bytes | bytearray):
        return [sanitize_details(item) for item in details]
    return details


def sanitize_text(value: str, *, fallback: str) -> str:
    if any(pattern.search(value) for pattern in SENSITIVE_PATTERNS):
        return fallback
    return value


def get_request_id(request: Request | HttpRequest | None) -> str:
    django_request = getattr(request, "_request", request)
    request_id = getattr(django_request, "request_id", None)
    return str(request_id) if request_id else "req_unknown"


def api_not_found(
    request: HttpRequest, exception: Exception | None = None
) -> JsonResponse:
    return build_error_json_response(
        code=ErrorCode.NOT_FOUND,
        message="Not found.",
        details=[],
        status_code=status.HTTP_404_NOT_FOUND,
        request=request,
    )


def api_server_error(request: HttpRequest) -> JsonResponse:
    return build_error_json_response(
        code=ErrorCode.INTERNAL_ERROR,
        message="Internal server error.",
        details=[],
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        request=request,
    )
