# Build Plan

## Tech Stack

### Backend
| package | purpose |
|---|---|
| FastAPI | Web framework — API routers, dependency injection, request/response handling |
| FastMCP | MCP server — exposes read tools to MCP clients |
| SQLAlchemy | ORM — model definitions and query building |
| Alembic | Database migrations |
| asyncpg | Async PostgreSQL driver used by SQLAlchemy |
| Pydantic | Data validation and serialisation (ships with FastAPI) |
| MSAL (msal) | Microsoft OAuth — authorization code flow, silent token acquisition, token cache serialisation |
| cryptography | Fernet symmetric encryption for the MSAL token cache at rest |
| PyJWT | Minting and validating app-level JWTs for web UI sessions |
| uvicorn | ASGI server |
| python-dotenv | Local `.env` loading |
| Surya OCR | Handwriting OCR — self-hosted, pip installable, containerisable, ~80-95% accuracy on clear handwriting. Upgrade path: TrOCR (Microsoft HuggingFace) if accuracy on real OneNote samples is insufficient |

Architecture layers (service/repository/API separation):
```
FastAPI routers       ← HTTP / MCP request handling only
    ↓
Service layer         ← business logic, orchestration
    ↓
Repository layer      ← all database queries (SQLAlchemy)
    ↓
Models                ← SQLAlchemy table definitions
```

### Frontend
| package | purpose |
|---|---|
| Vite + React | Build tooling and UI framework |
| TypeScript | Type safety |
| Tailwind CSS | Utility-first styling |
| shadcn/ui | Pre-built accessible components built on Radix UI |
| React Router | Client-side routing |
| TanStack Query | Server state — data fetching, caching, background refetch |
| Axios | HTTP client for API calls |

---

## Phase 1 — Local Backend

Goal: a fully working backend running on `localhost:8000` connected to a local Docker Postgres.

- Project setup with Poetry, folder structure, `.env`
- Docker Postgres container
- SQLAlchemy models, Alembic initial migration
- JWT auth middleware
- Microsoft OAuth flow (login + callback)
- Notebooks, MCP connections, and sync status API endpoints
- Sync command (`python -m sync.run`) — traverses Graph API, extracts text, runs Surya OCR on handwritten pages, writes combined content to DB
- FastMCP server mounted into FastAPI — search, get page, get page image, list notebooks tools

---

## Phase 2 — Local Frontend

Goal: a working React UI on `localhost:5173` connected to the local backend.

- Vite + React + TypeScript scaffold, Tailwind + shadcn setup
- Sign in with Microsoft flow, JWT stored in React context
- Notebooks page — list with sync status, enable/disable toggle
- MCP connections page — create connection, show token once, list and revoke
- Account page — connected Microsoft account, reconnect/disconnect, reauth warning

---

## Phase 3 — Microsoft App Registration

Do this while building Phase 2 so the OAuth flow can be tested end to end.

- Register app in Azure Entra ID
- Set type to **multi-tenant** (required for personal Microsoft accounts and accounts outside your own Azure AD)
- Add redirect URI: `http://localhost:8000/auth/microsoft/callback`
- Add delegated permissions: `openid`, `profile`, `email`, `offline_access`, `User.Read`, `Notes.Read`
- Add `client_id` and `client_secret` to `.env`
- Test full OAuth flow locally

---

## Phase 4 — Deployment

Deploy in chunks, verifying each before moving to the next.

- **Database** — create Neon Postgres, run Alembic migrations
- **Backend** — deploy FastAPI + FastMCP to Railway, set secrets, add production redirect URI to Azure app registration
- **Sync cron** — deploy sync command as Railway cron service, verify first run against Neon
- **Frontend** — deploy to Vercel or Cloudflare Pages, update backend CORS config, end-to-end test
