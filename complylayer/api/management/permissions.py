"""Permission classes.

Separate from `views` because DRF resolves `DEFAULT_PERMISSION_CLASSES` while
the settings module is loading, and `views` imports DRF — so pointing the
setting at `views` is a circular import that only shows up at startup.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission


class IsAuthenticatedKey(BasePermission):
    message = "Send an API key as `Authorization: Bearer cl_live_...`."

    def has_permission(self, request, view) -> bool:
        return getattr(request, "credentials", None) is not None
