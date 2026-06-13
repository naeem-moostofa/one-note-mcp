"""
End-to-end smoke test for the REST API layer (routers/me, notebooks, mcp_connections).

Seeds a minimal corpus (2 users, notebooks, a Microsoft connection), mints an app
JWT with create_jwt (no live OAuth needed), and drives the routers in-process via
httpx ASGITransport. Asserts the happy paths plus the security boundaries:
401 (no/invalid JWT), 403 (cross-user), 404 (missing id), 400 (foreign notebook_ids),
and that no secret (MSAL cache, token_hash) ever crosses the wire.

Does NOT exercise POST /api/notebooks/refresh — that needs live Microsoft Graph.

Usage:
    uv run python -m scripts.smoke_rest
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.auth import create_jwt
from app.core.database import AsyncSessionLocal, engine
from app.main import app
from app.models import MicrosoftConnection, MicrosoftConnectionStatus, Notebook, User

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("smoke_rest")


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {message}")
    log.info("  OK: %s", message)


async def _seed() -> dict[str, int]:
    async with AsyncSessionLocal() as s:
        user = User(microsoft_oid="rest-oid", email="rest@example.com", display_name="Rest User")
        other = User(microsoft_oid="rest-oid-2", email="rest2@example.com", display_name="Other User")
        s.add_all([user, other])
        await s.flush()

        # Microsoft connection for `user` (status ACTIVE) — `other` has none.
        s.add(MicrosoftConnection(
            user_id=user.id,
            encrypted_msal_token_cache="not-a-real-cache",
            status=MicrosoftConnectionStatus.ACTIVE,
        ))

        nb_enabled = Notebook(user_id=user.id, onenote_id="r-nb-a", display_name="Enabled NB", sync_enabled=True)
        nb_disabled = Notebook(user_id=user.id, onenote_id="r-nb-b", display_name="Disabled NB", sync_enabled=False)
        other_nb = Notebook(user_id=other.id, onenote_id="r-nb-c", display_name="Other's NB", sync_enabled=True)
        s.add_all([nb_enabled, nb_disabled, other_nb])
        await s.flush()

        ids = {
            "user_id": user.id,
            "other_id": other.id,
            "nb_enabled": nb_enabled.id,
            "nb_disabled": nb_disabled.id,
            "other_nb": other_nb.id,
        }
        await s.commit()
        return ids


async def _teardown(user_ids: list[int]) -> None:
    async with AsyncSessionLocal() as s:
        for uid in user_ids:
            user = await s.get(User, uid)
            if user:
                await s.delete(user)
        await s.commit()


async def _run(ids: dict[str, int]) -> None:
    token = create_jwt(ids["user_id"])
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # --- auth boundary ---
        r = await c.get("/api/me")
        _assert(r.status_code == 401, f"GET /api/me with no header → 401 (got {r.status_code})")
        r = await c.get("/api/me", headers={"Authorization": "Bearer garbage"})
        _assert(r.status_code == 401, f"GET /api/me with invalid token → 401 (got {r.status_code})")

        # --- GET /api/me ---
        r = await c.get("/api/me", headers=auth)
        _assert(r.status_code == 200, f"GET /api/me → 200 (got {r.status_code})")
        body = r.json()
        _assert(body["email"] == "rest@example.com", "me.email matches the seeded user")
        _assert(body["microsoft_status"] == "ACTIVE", "me.microsoft_status is ACTIVE")
        _assert("encrypted_msal_token_cache" not in body, "me payload never includes the MSAL cache")

        # --- GET /api/notebooks (all, including disabled) ---
        r = await c.get("/api/notebooks", headers=auth)
        _assert(r.status_code == 200, f"GET /api/notebooks → 200 (got {r.status_code})")
        nbs = r.json()
        _assert(len(nbs) == 2, f"returns both of the user's notebooks incl. disabled (got {len(nbs)})")
        _assert(all(n["sync_status"] == "PENDING" for n in nbs), "fresh notebooks carry non-null PENDING status")
        _assert(all("onenote_id" not in n and "user_id" not in n for n in nbs), "web notebook shape drops onenote_id/user_id")

        # --- PATCH /api/notebooks/{id} (204, no body — verify via the next GET) ---
        r = await c.patch(f"/api/notebooks/{ids['nb_enabled']}", headers=auth, json={"sync_enabled": False})
        _assert(r.status_code == 204, f"PATCH own notebook → 204 (got {r.status_code})")
        _assert(not r.content, "204 response carries no body")
        r = await c.get("/api/notebooks", headers=auth)
        flipped = next(n for n in r.json() if n["id"] == ids["nb_enabled"])
        _assert(flipped["sync_enabled"] is False, "the flip persists on the next GET")

        # cross-user 403 + missing 404
        r = await c.patch(f"/api/notebooks/{ids['other_nb']}", headers=auth, json={"sync_enabled": False})
        _assert(r.status_code == 403, f"PATCH another user's notebook → 403 (got {r.status_code})")
        r = await c.patch("/api/notebooks/999999", headers=auth, json={"sync_enabled": False})
        _assert(r.status_code == 404, f"PATCH nonexistent notebook → 404 (got {r.status_code})")

        # --- POST /api/mcp-connections ---
        r = await c.post("/api/mcp-connections", headers=auth, json={"scope_all_notebooks": True, "display_name": "rest smoke"})
        _assert(r.status_code == 201, f"POST mcp-connection → 201 (got {r.status_code})")
        created = r.json()
        _assert(created["raw_token"].startswith("onmcp_"), "create response carries the raw_token")
        _assert(created["mcp_url"].endswith("/mcp"), f"create response carries mcp_url (got {created.get('mcp_url')!r})")
        conn_id = created["id"]

        # foreign notebook_ids → 400
        r = await c.post("/api/mcp-connections", headers=auth, json={
            "scope_all_notebooks": False, "notebook_ids": [ids["other_nb"]],
        })
        _assert(r.status_code == 400, f"POST with foreign notebook_ids → 400 (got {r.status_code})")
        _assert(str(ids["other_nb"]) not in r.json()["detail"], "400 detail doesn't reveal which id was foreign")

        # --- GET /api/mcp-connections ---
        r = await c.get("/api/mcp-connections", headers=auth)
        _assert(r.status_code == 200, f"GET mcp-connections → 200 (got {r.status_code})")
        rows = r.json()
        _assert(len(rows) == 1, f"lists the one created connection (got {len(rows)})")
        _assert(all("token_hash" not in row for row in rows), "list never includes token_hash")
        _assert(all("raw_token" not in row and "mcp_url" not in row for row in rows), "list never repeats raw_token/mcp_url")

        # --- DELETE /api/mcp-connections/{id} ---
        r = await c.delete(f"/api/mcp-connections/999999", headers=auth)
        _assert(r.status_code == 404, f"DELETE nonexistent connection → 404 (got {r.status_code})")
        r = await c.delete(f"/api/mcp-connections/{conn_id}", headers=auth)
        _assert(r.status_code == 204, f"DELETE own connection → 204 (got {r.status_code})")
        r = await c.get("/api/mcp-connections", headers=auth)
        revoked = next(row for row in r.json() if row["id"] == conn_id)
        _assert(revoked["revoked_at"] is not None, "the connection now shows revoked_at set")


async def main() -> None:
    log.info("Seeding…")
    ids = await _seed()
    try:
        log.info("Running REST smoke checks…")
        await _run(ids)
        log.info("ALL CHECKS PASSED")
    finally:
        log.info("Tearing down…")
        await _teardown([ids["user_id"], ids["other_id"]])
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
