"""Errors on the decision path.

Different audience from the DSL error catalogue. These are read by an engineer
integrating the API, usually at 2am, usually from a log line — so they name the
field and say what was expected, and never leak what the tenant sent back into
the response.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, field: str = ""):
        self.code = code
        self.message = message
        self.status = status
        self.field = field
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload
