"""
Smoke test for the WorkOS-JWT resource-server path (web MCP connectors).

Exercises the parts of the OAuth flow that live in *our* code and can run
offline: JWT validation semantics (audience/expiry), the claim → user-id
extraction, and the token → notebook-scope resolution. The interactive WorkOS
half (DCR/CIMD → bridge login → completion → real token) requires the deployed
instance + dashboard and is verified live, not here.

By default a JWT is minted locally with a throwaway RSA key to drive the
verifier. If you have a real WorkOS access token, set WORKOS_TEST_JWT and the
script additionally runs it through the live composite provider (JWKS).

Verifies:
  - extract_user_id_from_claims: external_id preferred, numeric sub fallback,
    opaque subject → None
  - resolve_jwt_identity: all sync-enabled notebooks; unknown subject → None
    (401); onboarded user with nothing synced → empty scope (authenticated)
  - JWTVerifier: valid aud passes; wrong aud and expired both fail (→ 401)

Usage:
    uv run python -m scripts.smoke_oauth
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.mcp.identity import extract_user_id_from_claims, resolve_jwt_identity
from app.models import Notebook, User

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("smoke_oauth")

_ISSUER = "https://smoke-oauth.example.com"


async def _seed() -> dict[str, int]:
    """A user with one sync-enabled + one disabled notebook, plus a second user
    with nothing synced (the onboarded-but-empty case)."""
    async with AsyncSessionLocal() as session:
        user = User(microsoft_oid="smoke-oauth-oid", email="oauth@example.com", display_name="OAuth User")
        empty_user = User(microsoft_oid="smoke-oauth-oid-2", email="empty@example.com", display_name="Empty User")
        session.add_all([user, empty_user])
        await session.flush()

        enabled = Notebook(user_id=user.id, onenote_id="oa-nb-a", display_name="Synced", sync_enabled=True)
        disabled = Notebook(user_id=user.id, onenote_id="oa-nb-b", display_name="Off", sync_enabled=False)
        empty_disabled = Notebook(user_id=empty_user.id, onenote_id="oa-nb-c", display_name="Off2", sync_enabled=False)
        session.add_all([enabled, disabled, empty_disabled])
        await session.commit()
        return {
            "user_id": user.id,
            "empty_user_id": empty_user.id,
            "enabled_notebook": enabled.id,
        }


async def _teardown(user_ids: list[int]) -> None:
    async with AsyncSessionLocal() as session:
        for user_id in user_ids:
            user = await session.get(User, user_id)
            if user:
                await session.delete(user)
        await session.commit()


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {message}")
    log.info("  OK: %s", message)


async def _run(ids: dict[str, int]) -> None:
    user_id = ids["user_id"]

    # 1. Claim → user-id extraction (keyed solely on external_id; opaque sub ignored).
    _assert(extract_user_id_from_claims({"external_id": str(user_id), "sub": "user_xyz"}) == user_id,
            "users.id is read from the external_id claim")
    _assert(extract_user_id_from_claims({"sub": str(user_id)}) is None,
            "a numeric sub is NOT trusted — only external_id maps to a user")
    _assert(extract_user_id_from_claims({"sub": "user_opaque"}) is None,
            "missing external_id → None (→ 401)")

    # 2. Scope resolution.
    async with AsyncSessionLocal() as session:
        scope = await resolve_jwt_identity(user_id, session)
    _assert(scope is not None, "resolve_jwt_identity resolves a known user")
    assert scope is not None
    _assert(scope.user_id == user_id, "resolved scope carries the user id")
    _assert(scope.allowed_notebook_ids == [ids["enabled_notebook"]],
            "scope = sync-enabled notebooks only (disabled excluded)")

    async with AsyncSessionLocal() as session:
        missing = await resolve_jwt_identity(2_000_000_000, session)
    _assert(missing is None, "unknown subject → None (401, never auto-created)")

    async with AsyncSessionLocal() as session:
        empty = await resolve_jwt_identity(ids["empty_user_id"], session)
    _assert(empty is not None and empty.allowed_notebook_ids == [],
            "onboarded user with nothing synced → authenticated but empty scope")

    # 3. JWT validation semantics (audience must equal MCP_SERVER_URL; expiry enforced).
    keypair = RSAKeyPair.generate()
    verifier = JWTVerifier(
        public_key=keypair.public_key,
        issuer=_ISSUER,
        audience=settings.MCP_SERVER_URL,
        algorithm="RS256",
    )
    # AuthKit's sub is opaque; our users.id rides along as the external_id claim.
    external_id_claim = {"external_id": str(user_id)}
    good = keypair.create_token(
        subject="user_opaque", issuer=_ISSUER, audience=settings.MCP_SERVER_URL,
        additional_claims=external_id_claim,
    )
    access = await verifier.verify_token(good)
    _assert(access is not None, "JWT with correct audience validates")
    assert access is not None
    _assert(extract_user_id_from_claims(access.claims) == user_id,
            "validated JWT's external_id claim resolves back to the user id")

    wrong_aud = keypair.create_token(
        subject="user_opaque", issuer=_ISSUER, audience="https://attacker.example/mcp",
        additional_claims=external_id_claim,
    )
    _assert(await verifier.verify_token(wrong_aud) is None, "JWT with wrong audience is rejected (→ 401)")

    expired = keypair.create_token(
        subject="user_opaque", issuer=_ISSUER, audience=settings.MCP_SERVER_URL,
        expires_in_seconds=-60, additional_claims=external_id_claim,
    )
    _assert(await verifier.verify_token(expired) is None, "expired JWT is rejected (→ 401)")

    # 4. Optional: run a real WorkOS token through the live composite provider.
    import os

    real_token = os.environ.get("WORKOS_TEST_JWT")
    if real_token:
        from app.mcp.workos_auth import build_mcp_auth

        provider = build_mcp_auth()
        real_access = await provider.verify_token(real_token)
        _assert(real_access is not None, "live WorkOS JWT validates through the composite provider")
        assert real_access is not None
        log.info("  live token sub → user_id %s, scope %s",
                 real_access.client_id, real_access.claims.get("onenote_mcp_allowed_notebook_ids"))
    else:
        log.info("  (skip) set WORKOS_TEST_JWT to also test a real WorkOS token via JWKS")


async def main() -> None:
    ids = await _seed()
    try:
        await _run(ids)
        log.info("smoke_oauth: ALL PASSED")
    finally:
        await _teardown([ids["user_id"], ids["empty_user_id"]])
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
