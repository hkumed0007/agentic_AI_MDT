class NotAuthorizedError(Exception):
    """Raised when a tool is asked to touch data outside the locked patient."""


class ContextBudgetError(RuntimeError):
    """Raised when a prompt does not fit, or the model stopped because it hit its length limit.

    Raised instead of silently truncating: a truncated clinical record produces confident
    answers about a patient the model has only partly read.
    """


class StructuredOutputError(RuntimeError):
    """Raised when the model cannot produce schema-valid JSON after all retries."""
