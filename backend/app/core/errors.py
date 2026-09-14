from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str = "", **detail):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class Denied(DomainError):
    status_code = 403
    code = "denied"


class Blocked(DomainError):
    """A business gate blocks the action (not a permission problem)."""
    status_code = 409
    code = "blocked"


class Unsupported(DomainError):
    status_code = 501
    code = "unsupported"


class ProviderError(DomainError):
    status_code = 502
    code = "provider_error"
