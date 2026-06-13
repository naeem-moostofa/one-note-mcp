class MSALAuthError(Exception):
    """Raised when MSAL cannot acquire a token and user re-authentication is required."""


class GraphAPIError(Exception):
    """Raised when the Microsoft Graph API returns an unrecoverable error."""


class AppError(Exception):
    """Base for domain errors; each subclass sets status_code, read by the handler in main.py."""

    status_code = 500


class ResourceNotFoundError(AppError):
    """The resource does not exist."""

    status_code = 404


class ForbiddenError(AppError):
    """The resource exists but isn't owned by the caller."""

    status_code = 403


class InvalidRequestError(AppError):
    """Semantically invalid request the schema can't catch (e.g. unowned notebook_ids)."""

    status_code = 400


class ConflictError(AppError):
    """Well-formed request, but the account state doesn't allow it (e.g. no active connection)."""

    status_code = 409
