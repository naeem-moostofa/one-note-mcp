# Backend Plan

## Folder Structure

```
backend/
  app/
    routers/          # FastAPI route handlers (API layer)
    services/         # Business logic (service layer)
    repositories/     # Database queries (repository layer)
    clients/          # Third-party API wrappers (Graph, MSAL, OCR)
    models/           # SQLAlchemy table definitions
    schemas/          # Pydantic request/response shapes
    core/             # Config, DB session, auth middleware, CORS
    mcp/              # FastMCP server and tool definitions
  alembic/            # Migrations
  sync/               # Standalone sync entry point
  main.py             # App entrypoint — wires routers and mounts FastMCP
```

Call chain: `routers → services → repositories / clients`

Services call repositories and clients only — never other services.

**Layer boundaries use Pydantic models throughout.** SQLAlchemy models stay inside the repository layer and are never passed out. Everything crossing a boundary — repository → service, client → service, service → router, and router responses — uses Pydantic schemas. Primitive Python types (`int`, `str`, `bool`, etc.) are fine to pass directly without wrapping.

---

## Core

**`core/config.py`** — loads all environment variables via Pydantic settings. Single source of truth for config across the app and sync command.

**`core/database.py`** — creates the async SQLAlchemy engine and session factory. FastAPI routes get a DB session via dependency injection.

**`core/auth.py`** — FastAPI dependency that extracts and validates the JWT from `Authorization: Bearer`. Injects the current user into protected routes.

**`core/middleware.py`** — configures CORS middleware using `FRONTEND_ORIGIN`. Runs on every request before it reaches a router.

---

## API Layer

Routers handle HTTP only — validate the incoming request, call into a service, return the response. No business logic lives here.

### `routers/auth.py`
| endpoint | description |
|---|---|
| `GET /auth/microsoft/login` | Builds the Microsoft OAuth URL and returns a redirect |
| `GET /auth/microsoft/callback` | Receives the auth code from Microsoft, hands off to AuthService, returns JWT to frontend |
| `POST /auth/microsoft/disconnect` | Removes the user's Microsoft connection |

### `routers/me.py`
| endpoint | description |
|---|---|
| `GET /api/me` | Returns the current user's profile and Microsoft connection status |

### `routers/notebooks.py`
| endpoint | description |
|---|---|
| `GET /api/notebooks` | Lists all notebooks for the current user with sync status |
| `PATCH /api/notebooks/{id}` | Toggles `sync_enabled` on a notebook |

### `routers/mcp_connections.py`
| endpoint | description |
|---|---|
| `POST /api/mcp-connections` | Creates a new MCP connection, returns the raw token once |
| `GET /api/mcp-connections` | Lists all MCP connections for the current user |
| `DELETE /api/mcp-connections/{id}` | Revokes a connection (sets `revoked_at`) |

---

## MCP Server

**`mcp/server.py`** — FastMCP server instance, mounted into the FastAPI ASGI app in `main.py`. Sits at `/mcp`.

All tools share a common auth step: hash the incoming MCP token, look it up in `mcp_connections`, check it is not revoked, resolve the notebook scope.

All tools check the `sync_status` of the relevant notebooks and pages. If any are `syncing`, `stale`, or `failed`, the response includes a note that the data may be incomplete or out of date. The most recent available data is always returned regardless.

| tool | description |
|---|---|
| `list_notebooks` | Returns notebooks in scope for this MCP connection (id, display_name, sync status). Intended as a first call so the caller can pick the right notebook(s) before searching |
| `search_pages` | Searches page content within the provided notebook scope. Returns matching pages with snippets — short windows of text around each match. Required param: `notebook_ids` (caller chooses scope via `list_notebooks` first — avoids returning irrelevant matches from dozens of notebooks). Optional: `search_size` (chars on each side of match — default 80, max 250), `max_pages` (default 10, max 20), `max_snippets_per_page` (default 5, max 10). Uses Postgres FTS first; falls back to `pg_trgm` similarity for terms FTS misses (handles OCR errors like `painters` ↔ `pointers`). Tool description warns the caller that page content mixes typed text with OCR output — OCR portions may contain recognition errors that should be interpreted semantically. Includes staleness note if applicable |
| `get_page` | Returns the full `content` of a single page (typed text + OCR text in visual order, as written to the DB by the sync job). Same caveat as `search_pages` — content may contain OCR errors. Includes staleness note if applicable |
| `get_page_image` | Escape hatch: fetches the rendered composite page image (slide + ink) when text content is insufficient. Returns image content via MCP's `ImageContent` type — FastMCP handles base64 encoding. Intended to be invoked only when the caller explicitly needs to read a page visually (large context cost) |
| `list_sections` | Returns sections within a notebook in scope |

**Note:** FastMCP is mounted into the same ASGI app as FastAPI. One process, one port, one Railway service. FastAPI handles web routes; FastMCP handles MCP protocol at `/mcp`.

---

## Service Layer

Services contain all business logic. They call repositories and clients only — never other services.

### `services/auth_service.py`
- Build the Microsoft OAuth redirect URL using MSAL, generating a `state` parameter (stored temporarily to validate on callback and prevent CSRF)
- Handle the OAuth callback: validate `state`, exchange code for tokens via `msal_client`, extract `oid`/email/display_name from the ID token, upsert user, encrypt and store MSAL cache
- Create and validate app JWTs

### `services/notebook_service.py`
- List notebooks for a user
- Toggle `sync_enabled` on a notebook

### `services/mcp_connection_service.py`
- Generate a cryptographically random raw token, SHA-256 hash it, store the hash — return the raw token once
- List connections
- Revoke a connection
- Resolve an incoming MCP token: hash it, look it up, validate not revoked, return the connection with its notebook scope

### `services/search_service.py`

Accepts: `query`, `notebook_ids` (required, intersected with the MCP connection's allowed scope), `search_size`, `max_pages`, `max_snippets_per_page`.

Algorithm:
1. **FTS pass** — `to_tsquery` against `pages.search_vector`, scoped to the intersected notebook IDs. Returns matching page IDs with `ts_rank_cd` scores. High precision, fast.
2. **Trigram fallback** — for query terms that produced no FTS matches (typical for OCR-mangled words), run `similarity(content, term) > threshold` against the same notebook scope. Threshold ~0.3 catches most OCR drift without flooding results. Uses the `ix_pages_content_trgm` index.
3. **Rank + cap pages** — combine FTS rank and max trigram similarity per page; take top `max_pages` (default 10, max 20).
4. **Extract snippets per page** — for each top-ranked page, find match offsets in `content`. For each offset, take a window of `search_size` chars (default 80, max 250) on each side.
5. **Merge overlapping windows** — if two windows overlap or touch, merge into one larger window. Cheaper than returning near-duplicates.
6. **Cap snippets per page** — take top `max_snippets_per_page` (default 5, max 10) after merging.
7. **Join through sections to notebook** — return the full path (Notebook > Section > Page) plus the snippet list.

Title is intentionally not weighted into ranking for V1 — small expected gain for added complexity.

### `services/sync_service.py`
- Contains all sync logic — calls `graph_client`, `msal_client`, `ocr_client`, and repositories
- Entry point called by `sync/run.py`

---

## Client Layer

Clients are thin wrappers around third-party APIs and libraries. They handle transport concerns (auth headers, pagination, retries) so services never deal with them directly.

### `clients/graph_client.py`
Wraps Microsoft Graph API calls. Handles pagination (`@odata.nextLink`) and 429 rate limit responses (retry with backoff).

- Fetch notebooks
- Fetch sections for a notebook
- Fetch pages (metadata) for a section
- Fetch page content (HTML)
- Fetch page image

### `clients/msal_client.py`
Wraps MSAL Python. Handles token acquisition and cache serialisation.

- Build the Microsoft OAuth redirect URL
- Exchange an authorization code for tokens
- Acquire a token silently from a serialised cache (re-serialises the cache after acquisition so any internal token rotation is preserved)
- On MSAL failure (token revoked/expired beyond refresh) — raises so the caller can mark the connection `needs_reauth`

**Note:** After every silent token acquisition MSAL may internally rotate the refresh token. The updated serialised cache must be saved back to the DB even if nothing else changed — otherwise the next acquisition will fail.

### `clients/ocr_client.py`
Wraps Google Cloud Vision (`DOCUMENT_TEXT_DETECTION`). Authenticated via API key (`GOOGLE_CLOUD_VISION_API_KEY`).

- `run_ocr(image_bytes)` — single Vision call per page. Returns extracted text (empty string if none — not an error)

---

## Repository Layer

Repositories contain all SQL. They accept primitive types or Pydantic models and always return Pydantic models — SQLAlchemy models never leave this layer.

### `repositories/user_repository.py`
- Upsert user by `microsoft_oid`
- Get user by ID

### `repositories/microsoft_connection_repository.py`
- Get connection by user ID
- Upsert connection (create or update cache)
- Update encrypted cache (called after every token refresh)
- Set status to `needs_reauth`
- Delete by user ID

### `repositories/notebook_repository.py`
- List notebooks by user
- Upsert many (from Graph API results)
- Set `sync_enabled`
- Update `sync_status` and `last_synced_at`
- Delete notebooks whose `onenote_id` is no longer returned by Graph (handles deletions)

### `repositories/section_repository.py`
- Upsert many sections for a notebook
- Delete sections whose `onenote_id` is no longer returned by Graph

### `repositories/page_repository.py`
- Get page by `onenote_id`
- Upsert page (create or update)
- Update `content`, `content_hash`, `sync_status`, `last_synced_at` after a successful sync
- Full-text search on `search_vector` scoped to notebook IDs — returns page IDs + FTS rank scores
- Trigram fuzzy search on `content` scoped to notebook IDs — returns page IDs + similarity scores. Used as fallback for OCR-mangled terms
- Get single page by ID
- Delete pages whose `onenote_id` is no longer returned by Graph

### `repositories/mcp_connection_repository.py`
- Create connection
- List by user ID
- Get by token hash (for MCP auth)
- Set `revoked_at`

---

## Background Sync Job

Sync logic lives in `services/sync_service.py`. A thin standalone script acts as the entry point for local dev and the Railway cron.

**`sync/run.py`** — boots a DB session and calls `sync_service.run()`. Runs as a one-shot process completely separate from the FastAPI web server.

### Trigger

| environment | how it runs |
|---|---|
| Local dev | `python -m sync.run` — run manually after making changes in OneNote |
| Deployed | Railway cron service calls `python -m sync.run` on a fixed schedule (hourly or nightly) |

### Flow

```
For each active microsoft_connection:
    Acquire access token via msal_client (save updated cache back to DB)
    If MSAL failure → set status = needs_reauth, skip user

    Set all user's enabled notebooks to sync_status = syncing

    Fetch all notebooks from Graph
    Upsert notebooks to DB
    Delete DB notebooks no longer in Graph response

    For each sync-enabled notebook:
        Fetch sections from Graph
        Upsert sections to DB
        Delete DB sections no longer in Graph response

        For each section:
            Fetch page metadata from Graph (id, title, lastModifiedDateTime)
            Upsert any new pages to DB

            For each page where Graph lastModifiedDateTime > pages.last_synced_at:
                Fetch page HTML content from Graph
                Extract typed text blocks from HTML (in visual order)
                Check for ink/handwriting nodes in the HTML
                If composite needed (images and/or ink present):
                    Fetch all page images in parallel
                    Build composite canvas (slide images + ink overlay) — adaptive render scale to stay under Vision's per-image limit
                    Run a single OCR call against the composite via Vision API → ocr_text
                Combine typed text + OCR text in visual order → pages.content
                Update content_hash, sync_status = fresh, last_synced_at = now

            On page-level failure → set page sync_status = failed, continue

            Delete DB pages no longer in Graph response

        Update notebook sync_status = fresh, last_synced_at = now
        On notebook-level failure → set notebook sync_status = failed, continue
```

### Notes
- `pages.search_vector` is a Postgres generated column over `content` — it updates automatically when `content` is written. The sync job does not need to manage it.
- Graph API results are paginated. `graph_client.py` handles `@odata.nextLink` internally so the sync loop always sees complete results.
- The sync job continues processing other users/notebooks/pages on failure — a single failure should not abort the whole run.
- Notebooks are set to `syncing` at the start of the run. If the process crashes mid-run they remain `syncing`, which the MCP treats as stale and includes a note.

---

## Local Development

### Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop

### 1. Install dependencies
```bash
uv sync
```

### 2. Environment variables
Copy `.env.example` to `.env` and fill in the values.

```bash
# .env.example

# Microsoft OAuth (from Azure app registration)
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=
MICROSOFT_AUTHORITY=https://login.microsoftonline.com/common
MICROSOFT_REDIRECT_URI=http://localhost:8000/auth/microsoft/callback
MICROSOFT_SCOPES=openid profile email offline_access User.Read Notes.Read

# Database
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/onenote_mcp

# Encryption key for MSAL token cache — generate once with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY=

# Secret for signing app JWTs — any long random string
APP_SESSION_SECRET=

# Frontend origin for CORS
FRONTEND_ORIGIN=http://localhost:5173
```

### 3. Start Postgres
`docker-compose.yml` at the project root:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: onenote_mcp
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

```bash
docker compose up -d
```

### 4. Run migrations
```bash
alembic upgrade head
```

### 5. Start the backend
```bash
uvicorn app.main:app --reload --port 8000
```

API is now available at `http://localhost:8000`.

### 6. Run the sync command
Run manually whenever you want to pull in changes from OneNote:

```bash
python -m sync.run
```
