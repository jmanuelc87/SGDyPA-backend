from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


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
