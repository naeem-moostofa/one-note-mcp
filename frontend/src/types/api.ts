// Hand-written mirror of the backend's web-facing schemas (app/schemas.py).
// Keep field names/shapes in sync with the API. When this grows, consider
// generating it from the FastAPI OpenAPI schema:
//   pnpm dlx openapi-typescript http://localhost:8000/openapi.json -o src/types/api.ts

// Mirrors backend MicrosoftConnectionStatus (models.py).
export type MicrosoftConnectionStatus = 'ACTIVE' | 'NEEDS_REAUTH'

// GET /api/me  →  MeResponse
export interface MeResponse {
  id: number
  email: string
  display_name: string
  created_at: string // ISO 8601 datetime
  microsoft_status: MicrosoftConnectionStatus | null // null = no Microsoft account connected
}

// Mirrors backend NotebookSyncStatus (models.py). Orthogonal to sync_enabled.
export type NotebookSyncStatus = 'PENDING' | 'SYNCING' | 'FRESH' | 'FAILED'

// GET /api/notebooks  →  NotebookWebResponse[]
export interface NotebookWebResponse {
  id: number
  display_name: string
  sync_enabled: boolean
  sync_status: NotebookSyncStatus
  last_synced_at: string | null // ISO 8601 datetime; null until first content sync
  last_modified_datetime: string | null // ISO 8601; when the notebook was last edited in OneNote (null until first refresh)
}
