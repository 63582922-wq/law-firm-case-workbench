"""Safe terminal failures returned after a durable external submission.

Only identifiers and controlled error codes may cross this boundary. Provider
response bodies, exception text and credentials must never be persisted in an
Agent task receipt.
"""

from __future__ import annotations

import re
from uuid import UUID


_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


class CaseAgentKnownExternalFailure(RuntimeError):
    """A provider gave a definitive terminal response for an exact request."""

    def __init__(self, *, external_request_id: str, error_code: str) -> None:
        try:
            UUID(external_request_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("known external failure request id is invalid") from None
        if not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("known external failure code is invalid")
        self.external_request_id = external_request_id
        self.error_code = error_code
        super().__init__("external provider returned a terminal failure")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(external_request_id="
            "<bound>, error_code=<controlled>)"
        )


__all__ = ["CaseAgentKnownExternalFailure"]
