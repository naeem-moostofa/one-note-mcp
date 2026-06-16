import { Toggle } from '@/components/ui/toggle'
import { describeSyncStatus, formatTimestamp } from '@/features/notebooks/lib/sync-status'
import type { NotebookWebResponse } from '@/types/api'

interface NotebookCardProps {
  notebook: NotebookWebResponse
  onToggle: (id: number, syncEnabled: boolean) => void
  onSync: (id: number) => void
  disabled?: boolean
}

export function NotebookCard({ notebook, onToggle, onSync, disabled }: NotebookCardProps) {
  const status = describeSyncStatus(notebook)
  const isSyncing = notebook.sync_status === 'SYNCING'
  const accentClass = !notebook.sync_enabled
    ? 'border-l-muted'
    : notebook.sync_status === 'FAILED'
      ? 'border-l-warn'
      : isSyncing
        ? 'border-l-busy'
        : 'border-l-ok'

  return (
    <div className={`flex items-center justify-between gap-4 rounded-xl border border-l-4 border-line bg-surface px-5 py-4 transition-shadow hover:border-brand-soft hover:shadow-sm ${accentClass}`}>
      <div className="min-w-0">
        <p className="truncate font-medium text-ink">{notebook.display_name}</p>
        <span className={`mt-1 inline-block rounded-full px-2 py-0.5 text-xs font-medium ${status.badgeClass}`}>
          {status.label}
        </span>
        <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
          <div className="flex gap-1">
            <dt>Last edited:</dt>
            <dd className="text-ink">{formatTimestamp(notebook.last_modified_datetime)}</dd>
          </div>
          <div className="flex gap-1">
            <dt>Last synced:</dt>
            <dd className="text-ink">{formatTimestamp(notebook.last_synced_at)}</dd>
          </div>
        </dl>
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => onSync(notebook.id)}
          disabled={isSyncing}
          title="Sync this notebook's pages from OneNote now"
          className="rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSyncing ? 'Syncing…' : 'Sync'}
        </button>
        <Toggle
          checked={notebook.sync_enabled}
          onChange={(next) => onToggle(notebook.id, next)}
          disabled={disabled}
          label={`Toggle auto-sync for ${notebook.display_name}`}
        />
      </div>
    </div>
  )
}
