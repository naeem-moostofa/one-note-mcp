class MSALAuthError(Exception):
    """Raised when MSAL cannot acquire a token and user re-authentication is required."""


class GraphAPIError(Exception):
    """Raised when the Microsoft Graph API returns an unrecoverable error."""
