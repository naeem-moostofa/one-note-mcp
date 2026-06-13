# OneNote MCP

OneNote MCP is a local-first FastAPI/FastMCP backend that syncs Microsoft OneNote notebooks into PostgreSQL, combines typed content with OCR text, and exposes read-only notebook search tools to MCP clients. The current focus is the backend: Microsoft OAuth, notebook sync, Alembic-managed Postgres schema, and MCP tools mounted at `/mcp`.

## Dev Quickstart

Run Postgres from the project root:

```powershell
docker compose up -d
```
Starts the local PostgreSQL container used by the backend.

Run backend commands from `backend/`:

```powershell
cd backend
```
Moves into the Python backend project, where `pyproject.toml`, Alembic, and the app package live.

```powershell
uv sync
```
Installs/syncs the backend virtual environment from `pyproject.toml` and `uv.lock`.

```powershell
uv run alembic upgrade head
```
Applies all database migrations to the configured local Postgres database.

```powershell
uv run uvicorn app.main:app --reload --port 8000
```
Starts the FastAPI app with the mounted MCP server at `http://localhost:8000`.

Manual sync commands:

```powershell
uv run python -m sync.run
```
Runs a full OneNote sync for all active Microsoft connections.

```powershell
uv run python -m sync.run --notebooks-only
```
Refreshes the notebook list only, skipping section/page traversal and OCR.

```powershell
uv run python -m sync.run --notebook-id 1 --force
```
Forces a full resync of one notebook by its database ID.

Create `backend/.env` before migrations or startup; the app expects `POSTGRES_USER`, `POSTGRES_PASSWORD`, optional `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, plus Microsoft OAuth, token encryption, app session, frontend origin, and Google Cloud Vision API settings.
