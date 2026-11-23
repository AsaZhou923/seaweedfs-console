"""Small JSON field validators shared by API handlers."""

from typing import Any, Mapping

from .security import AppError


def required_string(payload: Mapping[str, Any], field: str, *, code: str = "INVALID_REQUEST") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AppError(code, f"{field} is required", 422, field=field)
    return value


def string_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AppError("INVALID_REQUEST", f"{field} must be a list of strings", 422, field=field)
    return value
