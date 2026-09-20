"""Consistent JSON error responses (CLAUDE.md section 22)."""

from __future__ import annotations


class ApiError(Exception):
    """Raised by service/route code for a well-defined, user-facing error."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=404)
