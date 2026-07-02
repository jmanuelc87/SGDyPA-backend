from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.identity import authentication


class KeycloakBearerAuthenticationMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            token = authentication.extract_bearer_token(
                request.headers.get("Authorization", "")
            )
            if token is not None:
                user, claims = authentication.authenticate_bearer_token(token)
                request.user = user
                request.keycloak_claims = claims
        except authentication.BearerAuthenticationError as exc:
            response = JsonResponse({"detail": exc.detail}, status=exc.status_code)
            response["WWW-Authenticate"] = 'Bearer realm="api"'
            return response

        return self.get_response(request)
