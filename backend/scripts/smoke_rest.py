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
    async with AsyncSessionLocal() as session:
        user = User(microsoft_oid="rest-oid", email="rest@example.com", display_name="Rest User")
        other = User(microsoft_oid="rest-oid-2", email="rest2@example.com", display_name="Other User")
        session.add_all([user, other])
        await session.flush()

        # Microsoft connection for `user` (status ACTIVE) — `other` has none.
        session.add(MicrosoftConnection(
            user_id=user.id,
            encrypted_msal_token_cache="not-a-real-cache",
            status=MicrosoftConnectionStatus.ACTIVE,
        ))

        enabled_notebook = Notebook(user_id=user.id, onenote_id="r-nb-a", display_name="Enabled NB", sync_enabled=True)
        disabled_notebook = Notebook(user_id=user.id, onenote_id="r-nb-b", display_name="Disabled NB", sync_enabled=False)
        other_notebook = Notebook(user_id=other.id, onenote_id="r-nb-c", display_name="Other's NB", sync_enabled=True)
        session.add_all([enabled_notebook, disabled_notebook, other_notebook])
        await session.flush()

        ids = {
            "user_id": user.id,
            "other_id": other.id,
            "enabled_notebook": enabled_notebook.id,
            "disabled_notebook": disabled_notebook.id,
            "other_notebook": other_notebook.id,
        }
        await session.commit()
        return ids


async def _teardown(user_ids: list[int]) -> None:
    async with AsyncSessionLocal() as session:
        for user_id in user_ids:
            user = await session.get(User, user_id)
            if user:
                await session.delete(user)
        await session.commit()


async def _run(ids: dict[str, int]) -> None:
    token = create_jwt(ids["user_id"])
    authorization_header = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # --- auth boundary ---
        response = await client.get("/api/me")
        _assert(response.status_code == 401, f"GET /api/me with no header → 401 (got {response.status_code})")
        response = await client.get("/api/me", headers={"Authorization": "Bearer garbage"})
        _assert(response.status_code == 401, f"GET /api/me with invalid token → 401 (got {response.status_code})")

        # --- GET /api/me ---
        response = await client.get("/api/me", headers=authorization_header)
        _assert(response.status_code == 200, f"GET /api/me → 200 (got {response.status_code})")
        body = response.json()
        _assert(body["email"] == "rest@example.com", "me.email matches the seeded user")
        _assert(body["microsoft_status"] == "ACTIVE", "me.microsoft_status is ACTIVE")
        _assert("encrypted_msal_token_cache" not in body, "me payload never includes the MSAL cache")

        # --- GET /api/notebooks (paginated envelope; all, including disabled) ---
        response = await client.get("/api/notebooks", headers=authorization_header)
        _assert(response.status_code == 200, f"GET /api/notebooks → 200 (got {response.status_code})")
        page = response.json()
        _assert(set(page.keys()) == {"data", "total", "limit", "offset"}, "response is the PaginatedResponse envelope")
        _assert(page["limit"] == 50 and page["offset"] == 0, f"defaults to limit=50, offset=0 (got {page['limit']}, {page['offset']})")
        notebooks = page["data"]
        _assert(page["total"] == 2, f"total counts both of the user's notebooks (got {page['total']})")
        _assert(len(notebooks) == 2, f"returns both of the user's notebooks incl. disabled (got {len(notebooks)})")
        _assert(all(notebook["sync_status"] == "PENDING" for notebook in notebooks), "fresh notebooks carry non-null PENDING status")
        _assert(all("onenote_id" not in notebook and "user_id" not in notebook for notebook in notebooks), "web notebook shape drops onenote_id/user_id")

        # alphabetical fallback order (both notebooks have NULL last_modified → name, id): "Disabled NB" before "Enabled NB"
        _assert([nb["display_name"] for nb in notebooks] == ["Disabled NB", "Enabled NB"], "rows are ordered deterministically (name fallback when last edited is NULL)")

        # --- pagination: limit/offset walk the sorted rows ---
        response = await client.get("/api/notebooks", headers=authorization_header, params={"limit": 1, "offset": 1})
        _assert(response.status_code == 200, f"GET with limit=1&offset=1 → 200 (got {response.status_code})")
        second_page = response.json()
        _assert(second_page["total"] == 2, "total stays the full match count regardless of limit/offset")
        _assert(len(second_page["data"]) == 1 and second_page["data"][0]["display_name"] == "Enabled NB", "limit=1&offset=1 returns the second sorted row")

        # --- filters apply before counting/paginating ---
        response = await client.get("/api/notebooks", headers=authorization_header, params={"sync_enabled": "false"})
        filtered = response.json()
        _assert(filtered["total"] == 1 and filtered["data"][0]["display_name"] == "Disabled NB", "sync_enabled=false filters before counting")
        response = await client.get("/api/notebooks", headers=authorization_header, params={"search": "Enabled"})
        searched = response.json()
        _assert(searched["total"] == 1 and searched["data"][0]["display_name"] == "Enabled NB", "search filters before counting")

        # --- pagination validation errors ---
        for bad_params in ({"limit": 0}, {"limit": 999}, {"offset": -1}):
            response = await client.get("/api/notebooks", headers=authorization_header, params=bad_params)
            _assert(response.status_code == 422, f"GET with {bad_params} → 422 validation error (got {response.status_code})")

        # --- PATCH /api/notebooks/{id} (authoritative updated notebook) ---
        response = await client.patch(f"/api/notebooks/{ids['enabled_notebook']}", headers=authorization_header, json={"sync_enabled": False})
        _assert(response.status_code == 200, f"PATCH own notebook → 200 (got {response.status_code})")
        updated = response.json()
        _assert(updated["id"] == ids["enabled_notebook"], "PATCH response returns the updated notebook")
        _assert(updated["sync_enabled"] is False, "PATCH response carries the authoritative sync_enabled value")
        response = await client.get("/api/notebooks", headers=authorization_header)
        flipped = next(notebook for notebook in response.json()["data"] if notebook["id"] == ids["enabled_notebook"])
        _assert(flipped["sync_enabled"] is False, "the flip persists on the next GET")

        # cross-user 403 + missing 404
        response = await client.patch(f"/api/notebooks/{ids['other_notebook']}", headers=authorization_header, json={"sync_enabled": False})
        _assert(response.status_code == 403, f"PATCH another user's notebook → 403 (got {response.status_code})")
        response = await client.patch("/api/notebooks/999999", headers=authorization_header, json={"sync_enabled": False})
        _assert(response.status_code == 404, f"PATCH nonexistent notebook → 404 (got {response.status_code})")

        # --- POST /api/mcp-connections ---
        response = await client.post("/api/mcp-connections", headers=authorization_header, json={"scope_all_notebooks": True, "display_name": "rest smoke"})
        _assert(response.status_code == 201, f"POST mcp-connection → 201 (got {response.status_code})")
        created = response.json()
        _assert(created["raw_token"].startswith("onmcp_"), "create response carries the raw_token")
        _assert(created["mcp_url"].endswith("/mcp"), f"create response carries mcp_url (got {created.get('mcp_url')!r})")
        conn_id = created["id"]

        # foreign notebook_ids → 400
        response = await client.post("/api/mcp-connections", headers=authorization_header, json={
            "scope_all_notebooks": False, "notebook_ids": [ids["other_notebook"]],
        })
        _assert(response.status_code == 400, f"POST with foreign notebook_ids → 400 (got {response.status_code})")
        _assert(str(ids["other_notebook"]) not in response.json()["detail"], "400 detail doesn't reveal which id was foreign")

        # --- GET /api/mcp-connections ---
        response = await client.get("/api/mcp-connections", headers=authorization_header)
        _assert(response.status_code == 200, f"GET mcp-connections → 200 (got {response.status_code})")
        rows = response.json()
        _assert(len(rows) == 1, f"lists the one created connection (got {len(rows)})")
        _assert(all("token_hash" not in row for row in rows), "list never includes token_hash")
        _assert(all("raw_token" not in row and "mcp_url" not in row for row in rows), "list never repeats raw_token/mcp_url")

        # --- DELETE /api/mcp-connections/{id} ---
        response = await client.delete(f"/api/mcp-connections/999999", headers=authorization_header)
        _assert(response.status_code == 404, f"DELETE nonexistent connection → 404 (got {response.status_code})")
        response = await client.delete(f"/api/mcp-connections/{conn_id}", headers=authorization_header)
        _assert(response.status_code == 204, f"DELETE own connection → 204 (got {response.status_code})")
        response = await client.get("/api/mcp-connections", headers=authorization_header)
        revoked = next(row for row in response.json() if row["id"] == conn_id)
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
