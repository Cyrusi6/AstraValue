class StructuredServiceError(RuntimeError):
    """Base error for the structured-data application boundary."""


class StructuredConflictError(StructuredServiceError):
    """Frozen identity/configuration conflicts with an existing run."""


class StructuredBusyError(StructuredServiceError):
    """A lease or bound store is temporarily unavailable."""


class StructuredIntegrityError(StructuredServiceError):
    """Persisted structured evidence failed an integrity check."""
