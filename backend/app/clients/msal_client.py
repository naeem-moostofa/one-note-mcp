from functools import lru_cache

import msal

from app.core.config import settings
from app.core.exceptions import MSALAuthError
from app.schemas import MSALAuthCodeFlow, MSALIDTokenClaims, MSALSilentTokenResult, MSALTokenResult


class MSALClient:
    def __init__(self) -> None:
        self._scopes = settings.MICROSOFT_SCOPES.split()

    def _build_app(
        self, serialized_cache: str | None = None
    ) -> tuple[msal.ConfidentialClientApplication, msal.SerializableTokenCache]:
        cache = msal.SerializableTokenCache()
        if serialized_cache:
            cache.deserialize(serialized_cache)
        app = msal.ConfidentialClientApplication(
            client_id=settings.MICROSOFT_CLIENT_ID,
            client_credential=settings.MICROSOFT_CLIENT_SECRET,
            authority=settings.MICROSOFT_AUTHORITY,
            token_cache=cache,
        )
        return app, cache

    def get_auth_code_flow(self, state: str) -> MSALAuthCodeFlow:
        """Returns the auth code flow. Caller must persist this between redirect and callback."""
        app, _ = self._build_app()
        flow = app.initiate_auth_code_flow(
            scopes=self._scopes,
            redirect_uri=settings.MICROSOFT_REDIRECT_URI,
            state=state,
            # Always show the account picker instead of silently reusing whichever
            # Microsoft session the browser already has — lets the user choose (and
            # makes connecting a different account possible).
            prompt="select_account",
        )
        return MSALAuthCodeFlow(**flow)

    def exchange_code(self, auth_code_flow: MSALAuthCodeFlow, auth_response: dict) -> MSALTokenResult:
        """Exchange an authorization code for tokens. Raises MSALAuthError on failure."""
        app, cache = self._build_app()
        try:
            result = app.acquire_token_by_auth_code_flow(
                auth_code_flow=auth_code_flow.model_dump(),
                auth_response=auth_response,
            )
        except ValueError as error:
            raise MSALAuthError(str(error)) from error
        if "error" in result:
            raise MSALAuthError(result.get("error_description", result["error"]))
        return MSALTokenResult(
            access_token=result["access_token"],
            id_token_claims=MSALIDTokenClaims(**result["id_token_claims"]),
            serialized_cache=cache.serialize(),
        )

    def acquire_token_silent(self, serialized_cache: str) -> MSALSilentTokenResult:
        """Acquire a token silently from a serialized cache.

        The updated cache must be saved back to the DB — MSAL may internally rotate
        the refresh token on every call. Raises MSALAuthError if re-auth is required.
        """
        app, cache = self._build_app(serialized_cache)
        accounts = app.get_accounts()
        if not accounts:
            raise MSALAuthError("No accounts in token cache — user must re-authenticate")
        result = app.acquire_token_silent(scopes=self._scopes, account=accounts[0])
        if not result or "error" in result:
            error_desc = (
                result.get("error_description", "Token acquisition failed") if result else "No result from MSAL"
            )
            raise MSALAuthError(error_desc)
        updated_cache = cache.serialize() if cache.has_state_changed else serialized_cache
        return MSALSilentTokenResult(access_token=result["access_token"], serialized_cache=updated_cache)


@lru_cache
def get_msal_client() -> MSALClient:
    return MSALClient()
