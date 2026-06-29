# Implementation Plan: OAuth 2.1 for claude.ai & ChatGPT web MCP connectors

**Goal:** make the OneNote MCP server connectable from **claude.ai (web)** and **ChatGPT
(web)** by fronting it with an OAuth 2.1 Authorization Server, **without rolling our own AS**
and **without disturbing the existing `onmcp_` bearer path** used by CLI clients (Claude Code
/ Cursor / Codex).

**Decided architecture (do not re-litigate):**
- **Authorization Server:** WorkOS AuthKit.
- **Login delegation:** WorkOS **Standalone Connect** ("bridge mode") — WorkOS handles the
  MCP-facing OAuth (DCR, CIMD, PKCE, token issuance, refresh) and **redirects users back to
  our app to authenticate via our existing Microsoft login + existing
  `MICROSOFT_REDIRECT_URI`**.
- **Identity:** we pass our internal `users.id` to WorkOS's completion API; it becomes the
  access-token `sub`. Request-time correlation is a primary-key lookup. Dedup stays keyed on
  `microsoft_oid` via the existing upsert.
- **Resource server:** FastMCP validates the WorkOS-issued JWT via JWKS. A **composite
  verifier** keeps the existing `onmcp_` token path for CLI clients.
- **Web connection scope:** all sync-enabled notebooks (no per-notebook picker for web).

This plan is written to be executed by an implementing agent. Steps marked **[HUMAN]**
require dashboard/account actions a human must do; steps marked **[VERIFY]** are
integration details to confirm against installed library versions / live behavior (a short
spike), not architectural unknowns.

---

## Background (why this is needed)

Today MCP auth is a static opaque bearer (`onmcp_…`, `MCPConnectionTokenVerifier`). CLI
clients accept a pasted token; **claude.ai and ChatGPT web do not** — they removed the paste
box, forbid tokens in URLs, and only speak the OAuth flow (discover → register/CIMD → user
authorizes with PKCE → redeem code → use token). So we must expose an OAuth 2.1 AS. WorkOS is
that AS; we keep our Microsoft login via Standalone Connect.

**Client compatibility reality:** ChatGPT web is the dependable target. **claude.ai web is
historically flaky** (documented client-side failures even against compliant servers) — treat
it as best-effort, verify live, don't promise it. Both are covered by WorkOS's **DCR + CIMD**
(ChatGPT prefers CIMD; claude.ai supports both).

---

## Prerequisites — manual setup

### [HUMAN] 1. WorkOS account + AuthKit
- Create a free WorkOS account (https://workos.com). Free to 1M MAU.
- Use a **single WorkOS environment / one set of credentials for both local dev and prod**.
  We deliberately do **not** split Staging/Production — that keeps a single Login URI and
  avoids the per-environment tunnel/ngrok dance.
- From the dashboard, capture (use the **same values** in the local `.env` and in prod
  secrets):
  - **API key** (`sk_...`) → env `WORKOS_API_KEY` (secret).
  - **Client ID** (`client_...`) → env `WORKOS_CLIENT_ID`.
  - **AuthKit domain** (the issuer, e.g. `https://<env>.authkit.app` or a custom domain) →
    env `WORKOS_AUTHKIT_DOMAIN`. JWKS + AS metadata are discovered from this.
  - **→ In code:** add these three to `backend/.env` (and blank placeholders to
    `backend/.env.example`). They're loaded by `Settings` in `app/core/config.py` (impl Step 1).

### [HUMAN] 2. Enable MCP + dynamic client onboarding
- In the WorkOS dashboard, enable the MCP / OAuth-server capability for the environment.
- Enable **both Dynamic Client Registration (DCR) and Client ID Metadata Document (CIMD)** so
  claude.ai and ChatGPT both connect. (WorkOS implements both; we just toggle them.)
- **→ In code:** nothing — dashboard toggles only.

### [HUMAN] 3. Configure Standalone Connect (the bridge)
- **Dashboard location:** https://dashboard.workos.com → **Connect → Configuration**. The
  OAuth application and the **Login URI** field both live in this **Connect** section (not
  under "Applications"). If there's no "Connect" nav item, the capability may need enabling
  for the environment first. (Ref: https://workos.com/docs/authkit/connect/standalone)
- Create an **OAuth application** in the dashboard (Standalone Connect).
- Set the single **Login URI** to our **deployed public HTTPS URL**:
  `https://<our-host>/oauth/bridge/login`. This is where WorkOS sends users with an
  `external_auth_id` param. (A **Production** environment requires HTTPS — `localhost` is only
  allowed in a **Sandbox** environment.)
- Because there's one Login URI and the web clients (claude.ai/ChatGPT) **cannot reach
  `localhost` for the MCP server anyway**, do all web-flow testing **against the deployed
  instance**, and iterate locally with Claude Code + the existing `onmcp_` path. (Optional:
  to debug the bridge purely locally you can temporarily repoint the Login URI to
  `http://localhost:<port>/oauth/bridge/login` — WorkOS redirects the *browser*, which is
  local, so this works — then switch it back. Not required for normal development.)
- **→ In code:** dashboard value only (not an env var); the URL must exactly match the
  `/oauth/bridge/login` route added in `app/routers/oauth_bridge.py` (impl Step 4).

### [HUMAN] 4. Microsoft Entra — no change
We keep our **existing Entra app registration and `MICROSOFT_REDIRECT_URI`**. Standalone
Connect delegates to *our* login, so WorkOS never talks to Microsoft and needs no Entra
redirect URI. (Contrast: the rejected "social connection" mode would have required adding a
WorkOS redirect URI to Entra and would only expose email, not `oid`.)
- **→ In code:** nothing — existing `MICROSOFT_*` vars in `backend/.env` stay as-is.

### [HUMAN] 5. Decide the MCP resource/audience
The token `aud` must equal our MCP server URL. Reuse the existing `MCP_SERVER_URL` setting as
the resource identifier; ensure clients are pointed at this exact URL.
- **→ In code:** already `MCP_SERVER_URL` in `backend/.env` (`.env.example` line 18) — no new
  var; consumed by the AuthKit provider in `app/mcp/server.py` (impl Step 2).

---

## Dependencies & imports

### Python packages (add to backend deps)
- `workos` — official WorkOS Python SDK, for the Standalone Connect **completion API** call.
- `fastmcp` — already present; **pin the version** and confirm it ships the AuthKit/remote
  provider (below). This auth surface has moved across releases.

### What we import
- WorkOS SDK: `from workos import WorkOSClient` (or module-level `import workos`), instantiated
  with `WORKOS_API_KEY` + `WORKOS_CLIENT_ID`. **[VERIFY]** exact client class + the
  Standalone-Connect completion method name in the installed SDK version.
- FastMCP auth provider for **AuthKit-as-AS (resource-server + JWKS, DCR/CIMD)**:
  `from fastmcp.server.auth.providers.workos import AuthKitProvider` **[VERIFY exact path]**.
  - **Use `AuthKitProvider` (RemoteAuthProvider/JWKS validation), NOT `WorkOSProvider`** —
    `WorkOSProvider` is the OAuth-**Proxy** pattern for a different use case and is the
    component that had the token-reuse CVE (audience not bound to `resource`). We want the
    resource-server-only provider that just validates WorkOS JWTs.
  - It needs `authkit_domain` (= `WORKOS_AUTHKIT_DOMAIN`) and `base_url` (= our MCP URL, for
    the `aud`/resource and PRM). **[VERIFY]** exact constructor kwargs.

---

## Architecture (confirmed Standalone Connect flow)

```mermaid
sequenceDiagram
    participant Cl as claude.ai / ChatGPT (web)
    participant RS as our /mcp (FastMCP resource server)
    participant WO as WorkOS AuthKit (AS)
    participant BR as our /oauth/bridge/login (Login URI)
    participant MS as Microsoft (existing callback)

    Cl->>RS: initialize (no token)
    RS-->>Cl: 401 WWW-Authenticate → PRM → WorkOS issuer
    Cl->>WO: discover AS metadata; register via DCR or present CIMD client_id; PKCE
    Cl->>WO: GET /oauth2/authorize
    WO->>BR: redirect user with ?external_auth_id=...
    BR->>MS: existing /auth/microsoft/login (same MICROSOFT_REDIRECT_URI)
    MS-->>BR: callback → read id_token.oid → upsert user (dedup on oid) → users.id
    BR->>WO: completion API { external_auth_id, id: users.id, email, first/last name }
    WO-->>BR: { redirect_uri }
    BR-->>Cl: redirect → WO shows consent → code → client
    Cl->>WO: POST /token (code + PKCE verifier) → JWT(sub=users.id) + refresh
    Cl->>RS: Authorization: Bearer <WorkOS JWT>
    RS->>RS: AuthKitProvider validates JWT (JWKS, iss, aud) → sub → get_by_id → scope
    RS-->>Cl: tool result
```

---

## Implementation steps (file-by-file)

### Step 1 — `app/core/config.py`: add settings
Add:
```python
WORKOS_API_KEY: str
WORKOS_CLIENT_ID: str
WORKOS_AUTHKIT_DOMAIN: str          # issuer; JWKS + AS metadata discovered from here
# MCP_SERVER_URL already exists → reused as the token audience / resource
```
Leave `MICROSOFT_*` untouched. Add the new keys to `.env` / deployment secrets.

### Step 2 — `app/mcp/server.py`: resource server + composite verifier
- Build the AuthKit provider for JWT validation, and compose it with the existing
  `MCPConnectionTokenVerifier` so both token types work:
  1. **Looks like a JWT** (3 dot-separated segments) → validate via AuthKit/JWKS (`iss` =
     `WORKOS_AUTHKIT_DOMAIN`, `aud` = `MCP_SERVER_URL`, `exp`). On success read `sub`
     (= our `users.id`), resolve scope (Step 3), return `AccessToken` with the **same claims
     shape used today** (`_CLAIM_CONNECTION_ID`, `_CLAIM_ALLOWED_NOTEBOOK_IDS`).
  2. **Else** (`onmcp_…`) → existing `MCPConnectionTokenVerifier.verify_token` path,
     **unchanged**.
- Both converge on `(user_id, allowed_notebook_ids)`, so `app/mcp/tools.py` and
  `current_scope()` are **untouched**.
- **[VERIFY]** that FastMCP serves the Protected Resource Metadata + `401 WWW-Authenticate`
  once an auth provider is set, and that the PRM is reachable where claude.ai probes (root
  `/.well-known/oauth-protected-resource`, and any `/mcp`-suffixed variant). A misplaced PRM
  makes claude.ai 404 and fail.
- Implementation choice for the composite: either a small custom `TokenVerifier` that
  delegates, or set the AuthKit provider as primary and call the `onmcp_` verifier in a
  fallback branch. Keep `current_scope()`'s output contract identical.

### Step 3 — `app/mcp/identity.py` (new): token → scope
```python
async def resolve_jwt_identity(user_id: int, session) -> ResolvedMCPConnection | None:
    # user = UserRepository(session).get_by_id(user_id); None → return None (un-onboarded)
    # notebooks = NotebookRepository(session).list_by_user(user_id)
    # allowed = sorted(n.id for n in notebooks if n.sync_enabled)   # web scope = all sync-enabled
    # return ResolvedMCPConnection(connection_id=<sentinel/0>, user_id=user_id, allowed_notebook_ids=allowed)
```
Reuses the exact scope logic from `MCPConnectionService.resolve_token`'s
`scope_all_notebooks` branch. No `mcp_connections` row for web connections (the WorkOS grant
*is* the connection). The `connection_id` claim can be a sentinel (web grants aren't revoked
per-row here; revocation is WorkOS-side).

### Step 4 — `app/routers/oauth_bridge.py` (new): the Login URI
- `GET /oauth/bridge/login`:
  1. Read `external_auth_id` from query; stash it in a short-lived **signed cookie** (reuse
     the `encrypt()` pattern from `routers/auth.py`'s `oauth_flow` cookie).
  2. Kick off the **existing Microsoft login** (reuse `AuthService.begin_login` /
     `routers/auth.py` login machinery) in **bridge mode** (carry a marker so the callback
     returns here, not to the SPA).
- Completion (after Microsoft callback resolves the user — see Step 5):
  3. Call the WorkOS **Standalone Connect completion API** with `external_auth_id` and
     `{ id: str(user.id), email: user.email, first_name, last_name }`. **[VERIFY]** exact SDK
     method (e.g. `workos.user_management.<complete_standalone_connect>` / a direct POST to
     the documented completion endpoint).
  4. Receive `redirect_uri` from the response → `RedirectResponse(redirect_uri)`.
- **[VERIFY]** that the token `sub` carries our passed `id` (WorkOS docs example shows
  `sub: "user_123"`). If WorkOS instead issues its own subject, add a `workos_user_id` column
  to `users` and map at completion time — Step 3 then looks up by that. Decide in the spike.

### Step 5 — `app/routers/auth.py`: bridge-aware Microsoft callback
- Add a **bridge mode** to the existing Microsoft login/callback (flag carried in the
  state/cookie). When set, after `AuthService.complete_login` (which already upserts the user
  by `microsoft_oid` and returns the user), **return control to the bridge** (Step 4's
  completion) instead of the normal `RedirectResponse(f"{FRONTEND_ORIGIN}?token=...")`.
- `complete_login` already gives us the deduped `users` row; expose its `id`/email to the
  bridge (e.g. return the user, or set it on request state / a signed cookie).
- **Normal SPA login path is unchanged.**

### Step 6 — `app/main.py`: wire it up
- `app.include_router(oauth_bridge.router)`.
- Confirm the FastMCP PRM is exposed and publicly fetchable (Step 2 [VERIFY]).
- No CORS change needed for the bridge (top-level browser navigation, not SPA fetch); ensure
  the `.well-known` metadata is publicly reachable.

### Unchanged (do not touch)
`MCPConnectionService` (incl. `resolve_token`, `create`), the `onmcp_` keys, the scoped-keys
UI, `app/mcp/tools.py`, `current_scope()`, the sync worker, and the MSAL cache. The MCP path
**never** calls Graph or touches MSAL — tools read synced Postgres data; we only need
`user_id` from the token.

---

## Identity & deduplication

Dedup key is **`microsoft_oid`** (immutable Microsoft subject), enforced by
`users.microsoft_oid UNIQUE` (`models.py:87`) and `UserRepository.upsert`'s
`on_conflict_do_update(index_elements=["microsoft_oid"])` (`user_repository.py:17`). `email`
is also `UNIQUE` but is a backstop, not the matching key.

- **A web user who already signed in on the web, then connects a client:** the bridge reuses
  `auth_service.complete_login` → oid-keyed upsert → **existing row returned, no duplicate**.
- **Correlation is per-token, not per-request:** Microsoft login + dedup happen **once** at
  authorize time; the JWT then carries `sub = users.id`; every MCP request is
  `get_by_id(sub)`. No email matching, no Graph, no MSAL on the hot path.
- **Un-onboarded user** (authenticates but has no `users` row / no synced notebooks): Step 3
  returns `None`/empty — **never auto-create**. Tools return empty with an implicit "connect
  and sync in the app first."

---

## Environment variables (summary)

| Var | Source | Notes |
|---|---|---|
| `WORKOS_API_KEY` | WorkOS dashboard | secret |
| `WORKOS_CLIENT_ID` | WorkOS dashboard | |
| `WORKOS_AUTHKIT_DOMAIN` | WorkOS dashboard | issuer; JWKS/metadata discovery |
| `MCP_SERVER_URL` | existing | reused as token audience/resource |
| `MICROSOFT_*` | existing | unchanged |

## WorkOS dashboard checklist (human, one-time)
- [ ] Environment created; API key + client id + AuthKit domain captured.
- [ ] MCP/OAuth-server capability enabled.
- [ ] **DCR and CIMD both enabled.**
- [ ] Standalone Connect OAuth application created.
- [ ] Single Login URI set to the deployed `https://<host>/oauth/bridge/login` (one
      environment shared by local dev and prod).

---

## Testing

- **Phase 0 spike (throwaway, do first):** with the dashboard configured, run a generic MCP
  client (or `mcp-remote` / Claude Code) through DCR/CIMD → `/oauth2/authorize` → bridge login
  (stub or real Microsoft) → completion API → `/token`. **Confirm the two [VERIFY] unknowns:**
  (a) token `sub` == the `id` we passed; (b) FastMCP `AuthKitProvider` import path + kwargs and
  that PRM/`401` are emitted. Decide `workos_user_id` column yes/no here.
- **`scripts/smoke_oauth.py` (new):** validate a real WorkOS JWT against the resource server,
  call a tool; assert invalid/expired/wrong-`aud` JWTs 401 and an unknown `sub` yields empty.
- **`scripts/smoke_mcp.py` (existing):** unchanged — proves the `onmcp_` CLI path still works.
- **Manual, in confidence order:** ChatGPT web (acceptance target) → Claude Code → claude.ai
  web (best-effort). Verify Microsoft login + consent, then `onenote_list_notebooks` /
  `onenote_search_pages` scoped to the user's sync-enabled notebooks; confirm a returning web
  user does not create a duplicate `users` row.

---

## Phasing

0. **Spike** the two [VERIFY] unknowns + WorkOS dashboard wiring (gate).
1. **Config + resource server:** Step 1–2 (AuthKit JWKS validation + composite verifier);
   confirm PRM/`401` and that the `onmcp_` path still works.
2. **Bridge + Microsoft reuse:** Step 4–5 (Login URI, `external_auth_id` cookie, completion
   API, bridge-aware callback).
3. **Identity mapping:** Step 3 + un-onboarded guard.
4. **Smoke tests + live connect** (ChatGPT first, then claude.ai).

---

## Risks & open items
- **claude.ai web may fail even when compliant** (client-side). Mitigation: strict compliance
  via WorkOS; commit only to ChatGPT.
- **[VERIFY] token `sub` mapping** — fallback is a `workos_user_id` column (decided in spike).
- **[VERIFY] FastMCP `AuthKitProvider` import path / kwargs** — pin FastMCP; do not use the
  OAuth-Proxy `WorkOSProvider`.
- **[VERIFY] WorkOS SDK completion method name** for Standalone Connect.
- **Vendor dependency** on WorkOS in the login path (free tier generous; bounded blast radius
  — it issues read-only-notebook MCP tokens, never holds Microsoft/Graph creds).
- **Single WorkOS environment / one Login URI** shared by local dev and prod (chosen to
  avoid the tunnel dance). Consequence: web-client flow testing happens against the deployed
  instance — fine, since claude.ai/ChatGPT need a public MCP URL regardless. Trade-off: no
  separate staging isolation; acceptable for this project's scale.

## Out of scope / deferred
- Per-connection notebook scoping for web (web = all sync-enabled by decision).
- Self-hosting the AS (Ory Hydra/Authlib) — considered and rejected in favor of WorkOS;
  revisit only if dropping the vendor becomes necessary.
- Connector Directory submission (public listing) — separate effort.
- Frontend changes — none; flow is WorkOS-hosted + our server-side bridge. The `mcp-keys` UI
  keeps minting manual CLI keys.

## Dependencies
- Builds on the MCP server (`mcp-server-plan.md`) and connection/token model
  (`mcp-keys-plan.md`, retained for CLI).
- Reuses Microsoft login (`auth_service`, `routers/auth.py`) for the bridge's auth step —
  same `MICROSOFT_REDIRECT_URI`, same Entra app.
- Requires a public **HTTPS** deployment and a WorkOS account.
