from rest_framework.exceptions import APIException

from apps.platform.error_codes import ErrorCode


class StableAPIException(APIException):
    code = ErrorCode.INTERNAL_ERROR
    default_detail = "The request could not be completed."
    status_code = 400

    def __init__(
        self,
        *,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: object | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.details = [] if details is None else details
        super().__init__(detail=message, code=code.value)
