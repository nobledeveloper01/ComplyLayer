"""Resolving the API key on every management request.

Middleware rather than a DRF authentication class so the decision path — which
is not DRF (D1) — can use the identical resolution. One implementation of "which
tenant is this?" is one place for it to be wrong.
"""

from __future__ import annotations

from django.http import JsonResponse

from complylayer.api.auth import AuthenticationFailed, authenticate
from complylayer.tenancy import tenant_scope

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


class ApiKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.credentials = None

        if request.path in EXEMPT_PATHS:
            return self.get_response(request)

        header = request.headers.get("Authorization", "")
        if header:
            try:
                request.credentials = authenticate(header)
            except AuthenticationFailed as exc:
                return JsonResponse({"error": "unauthorized", "message": str(exc)}, status=401)

        if request.credentials is None:
            # No key presented. DRF answers 403 at the permission class; there is
            # no tenant to scope to, and under RLS every query returns nothing
            # anyway, which is the safe direction.
            return self.get_response(request)

        # Sets `complylayer.tenant_id` for the life of this request, which is
        # what makes the row level security policies do anything. Nothing called
        # `tenant_scope` outside the tests before this, so on a non-superuser
        # connection every management query matched no rows — the third
        # isolation layer could not be switched on.
        with tenant_scope(request.credentials.tenant_id):
            return self.get_response(request)
