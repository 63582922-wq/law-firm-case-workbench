"""Explicit failures returned by the deterministic workflow kernel."""


class WorkflowError(Exception):
    """Base class for domain-control failures."""


class AuthorizationDenied(WorkflowError):
    """The actor's matter role cannot perform a protected command."""


class InvalidTransition(WorkflowError):
    """The requested lifecycle transition is not allowed from the current state."""


class VersionConflict(WorkflowError):
    """The command was based on an obsolete matter version."""


class IdempotencyConflict(WorkflowError):
    """A reused idempotency key carries different command input."""


class PreconditionBlocked(WorkflowError):
    """A protected command is missing an explicit required precondition."""
