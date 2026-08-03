"""Shared application/API errors with stable transport semantics."""


class RepositoryLookupTimeoutError(TimeoutError):
    """The repository metadata deadline passed before any add could commit."""
