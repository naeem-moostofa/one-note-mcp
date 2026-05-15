OneNote MCP Project Requirements, Data Model, and Deployment Notes

Requirements

MCP Connection

Users should be able to create an MCP connection in Cursor, Claude Code, or Codex to get access to their documents.

In the web interface, users should be able to create MCP connections scoped to specific notebooks. The default connection is to all notebooks.

The MCP exposes common search tools, including the ability to get the raw image of a page for handwritten text.

The MCP only exposes read tools, with no ability to write to OneNote documents.

Web Interface / Backend Processing

Users can sign in with Microsoft and grant permission to their OneNote account/notebooks.

The app periodically syncs enabled notebooks using the Microsoft Graph API. During each sync, text is extracted from updated pages using OCR. Only pages that have changed since the previous sync get rescanned using OCR.

Users have the ability to flag notebooks that will not automatically be synced. These notebooks will not show up as notebooks that the model knows about in the MCP connection. This applies to all MCP connections.

If the OCR/cache is stale or a sync is currently running, the MCP will return the most recent available data with a note indicating that the data may be stale.

Tech Stack

Current working recommendation; still subject to change.

Frontend: React + Vite

Web backend: FastAPI

MCP server: FastMCP

Database: PostgreSQL

Microsoft integration: Microsoft Graph API

OCR/sync processing: shared Python sync command, run manually in local development and as a Railway cron service in deployment

Search: PostgreSQL lexical/full-text search first; no embeddings for V1

Sync mechanism: scheduled sync using Railway cron

Page image retrieval: fetch/render on demand if feasible

Deployment: Railway for FastAPI, FastMCP, and scheduled sync/OCR jobs

OAuth / Token Handling

Use backend-owned Microsoft OAuth.

The frontend starts the Microsoft connection flow by redirecting the user to the backend. The FastAPI backend handles the Microsoft OAuth callback, exchanges the authorization code for tokens using MSAL Python, and stores an encrypted serialized MSAL token cache.

The frontend and MCP clients should never receive Microsoft access tokens, refresh tokens, or the MSAL token cache.

The Railway sync/OCR cron service loads the encrypted MSAL token cache, decrypts it, uses MSAL to silently acquire or refresh a Microsoft Graph access token, and then reads OneNote content through Microsoft Graph.

The MCP connection token is separate from Microsoft OAuth. MCP clients only receive an app-level read-only MCP token scoped to the notebooks selected by the user.

Recommended Microsoft OAuth scopes for V1:

openid

profile

email

offline_access

User.Read

Notes.Read

Avoid OneNote write scopes such as Notes.ReadWrite or Notes.ReadWrite.All.

Configuration, Secrets, and Per-User Stored Data

The project should distinguish between:

App-level secrets

App-level non-secret configuration

Per-user encrypted database data

Per-user application data

Microsoft tokens are not stored as environment variables. Each connected user has their own encrypted MSAL token cache stored in the database.

App-Level Secrets

Stored in Railway service variables/secrets or the equivalent managed secret store.

Railway Service: FastAPI + FastMCP

MICROSOFT_CLIENT_SECRET

DATABASE_URL

TOKEN_ENCRYPTION_KEY

APP_SESSION_SECRET

Railway Cron Service: sync/OCR worker

MICROSOFT_CLIENT_SECRET

DATABASE_URL

TOKEN_ENCRYPTION_KEY

OCR provider secret, if using external OCR

App-Level Non-Secret Configuration

Stored as normal environment variables.

Railway Service: FastAPI + FastMCP

MICROSOFT_CLIENT_ID

MICROSOFT_AUTHORITY or MICROSOFT_TENANT_ID

MICROSOFT_REDIRECT_URI

MICROSOFT_SCOPES

FRONTEND_ORIGIN

Railway Cron Service: sync/OCR worker

MICROSOFT_CLIENT_ID

MICROSOFT_AUTHORITY or MICROSOFT_TENANT_ID

MICROSOFT_SCOPES

OCR provider endpoint, if using external OCR

Per-User Encrypted Database Data

Stored in the database per connected user.

microsoft_connections.encrypted_msal_token_cache

Per-User Application Data

Stored in normal database tables.

Users

Microsoft connection records

Granted Microsoft scopes and account metadata

Notebooks

Sections

Pages

Notebook exclusions

MCP connections and notebook scopes

Page OCR text

Page typed text

Page sync status

OCR freshness metadata

MCP Token Storage

Generate a random MCP token when the user creates an MCP connection.

Show the raw token to the user once.

Store only a hash of the token in the database.

Associate the token with its allowed notebook scope.

Allow users to revoke/delete MCP connections from the web interface.

Local Development Plan

V1 should be built locally before deploying.

Local services:

React + Vite frontend on localhost:5173

FastAPI + FastMCP backend on localhost:8000

PostgreSQL running locally through Docker

Manual sync/OCR command run locally

Microsoft OAuth configured with a localhost redirect URI

The local sync/OCR command should use the same core sync code that will later run inside the Railway cron service.

Local development flow:

User starts the local frontend and backend.

User connects Microsoft through the backend OAuth flow.

Backend stores the encrypted MSAL token cache in local Postgres.

User enables notebooks for syncing.

Developer runs the sync/OCR command manually.

MCP clients connect to the local FastMCP endpoint.

Once the local flow works, the same backend and sync code can be deployed to Railway.

Data Model

To be developed.

Deployment

Main Application Backend

Use FastAPI on Railway for the main web backend.

The FastAPI backend handles:

Microsoft sign-in / OAuth flow

Microsoft Graph account connection

Notebook listing and notebook exclusion settings

MCP connection creation and scoping

Sync status APIs for the web interface

Internal APIs used by the sync/OCR process if needed

MCP Server

Use FastMCP for the MCP server.

For V1, deploy FastMCP in the same Railway service as FastAPI, either mounted into the same ASGI app or run from the same codebase.

The MCP server reads from the database/cache and exposes only read tools.

The MCP server should not run OCR during normal MCP requests.

Background Sync / OCR Processing

Use a Railway cron service for scheduled sync and OCR processing.

Use Railway cron to run the sync/OCR command on a fixed schedule, such as hourly or nightly.

Recommended flow:

Railway cron
        ↓
Run sync/OCR command
        ↓
Sync service scans enabled notebooks
        ↓
Sync service identifies pages changed since the previous sync
        ↓
Sync service extracts typed text and runs OCR only for changed pages
        ↓
Sync service updates page text, OCR status, and freshness metadata
        ↓
Job exits

The sync/OCR job handles:

Scanning only notebooks enabled for syncing

Fetching OneNote notebook, section, page, and content data from Microsoft Graph

Identifying changed pages using stored metadata and content hashes

Extracting typed text from page HTML

Fetching/rendering page images or handwriting content when needed

Running OCR only for changed pages

Updating page text, OCR status, and freshness metadata

The sync/OCR job should not run as part of normal MCP requests.

Database

Use PostgreSQL as the main database.

Current preferred hosted option: Neon Postgres.

Reasoning:

It is closer to “just Postgres.”

It avoids mixing Supabase Auth with Microsoft OAuth / Graph permissions.

It keeps the architecture cleaner for a custom Microsoft account connection flow.

Supabase Postgres remains a reasonable alternative if dashboard/platform convenience becomes more important.

Sync State

Do not use a separate job table for V1.

Store sync state directly on notebook and page records.

Notebook/page records should include fields such as:

sync status

last synced time

last OCR completed time

last seen OneNote modified time

content hash

last error

Possible statuses:

fresh

syncing

stale

failed

excluded

Frontend

Use React + Vite for the frontend.

Preferred local development: React + Vite dev server.

Deployment options:

Vercel or Cloudflare Pages for simplest static frontend hosting

Railway if keeping the frontend and backend in the same Railway project is preferred

The frontend handles:

Microsoft sign-in entry point

Notebook visibility/sync settings

MCP connection creation

Sync/OCR status display

Page Images

For V1, page images should be fetched or rendered on demand.

Do not add Cloudflare R2 or another object storage service for page images unless on-demand fetching/rendering proves too slow or expensive.

Local to Deployed Migration Path

Build and test locally with Docker Postgres.

Create the Neon Postgres database.

Run migrations against Neon.

Deploy the FastAPI + FastMCP service to Railway.

Add the production Microsoft OAuth redirect URI.

Deploy the sync/OCR command as a Railway cron service.

Configure Railway cron to run the sync/OCR command hourly or nightly.

Update frontend environment variables to point to the deployed backend.

Final V1 Deployment Summary

Local-first development with Docker Postgres, local FastAPI + FastMCP, local React + Vite, and a manual sync/OCR command

Web backend: FastAPI on Railway

MCP server: FastMCP on Railway

Background sync/OCR: shared Python sync command deployed as a Railway cron service and triggered hourly or nightly

Frontend: React + Vite, deployed to Vercel, Cloudflare Pages, or Railway

Database: Neon Postgres

Sync mechanism: scheduled sync with status fields on notebook/page records

Page images: fetch/render on demand

Object storage: not included in V1

