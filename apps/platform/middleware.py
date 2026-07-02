from collections.abc import Callable
from uuid import uuid4

from django.http import HttpRequest, HttpResponse


class RequestIDMiddleware:
    header_name = "HTTP_X_REQUEST_ID"
    response_header = "X-Request-Id"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.META.get(self.header_name) or f"req_{uuid4().hex}"
        request.request_id = request_id
        response = self.get_response(request)
        response[self.response_header] = request_id
        return response
