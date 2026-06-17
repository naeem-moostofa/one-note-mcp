import { Toggle } from '@/components/ui/toggle'
import { describeSyncStatus, formatTimestamp } from '@/features/notebooks/lib/sync-status'
import type { NotebookWebResponse } from '@/types/api'

interface NotebookCardProps {
  notebook: NotebookWebResponse
  onToggle: (id: number, syncEnabled: boolean) => void
  onSync: (id: number) => void
  disabled?: boolean
  selected?: boolean
  onSelectChange?: (notebook: NotebookWebResponse, selected: boolean) => void
  selectionDisabled?: boolean
}

export function NotebookCard({
  notebook,
  onToggle,
  onSync,
  disabled,
  selected = false,
  onSelectChange,
  selectionDisabled = false,
}: NotebookCardProps) {
  const status = describeSyncStatus(notebook)
  const isSyncing = notebook.sync_status === 'SYNCING'
  const syncDisabled = isSyncing || !notebook.sync_enabled
  const accentClass = !notebook.sync_enabled
    ? 'border-l-muted'
    : notebook.sync_status === 'FAILED'
      ? 'border-l-warn'
      : isSyncing
        ? 'border-l-busy'
        : 'border-l-ok'

  return (
    <div className={`flex items-center justify-between gap-4 rounded-xl border border-l-4 border-line bg-surface px-5 py-4 transition-shadow hover:border-brand-soft hover:shadow-sm ${accentClass}`}>
      <div className="flex min-w-0 items-start gap-3">
        <label
          className="mt-0.5 inline-flex shrink-0 items-center"
          title={selectionDisabled ? 'All notebooks is selected' : 'Include this notebook in a new MCP key'}
        >
          <input
            type="checkbox"
            checked={selected}
            disabled={selectionDisabled}
            onChange={(event) => onSelectChange?.(notebook, event.target.checked)}
            aria-label={`Select ${notebook.display_name} for a new MCP key`}
            className="h-4 w-4 accent-brand disabled:cursor-not-allowed disabled:opacity-50"
          />
        </label>
        <div className="min-w-0">
          <p className="truncate font-medium text-ink">{notebook.display_name}</p>
          <div className="mt-1 flex h-5 min-w-0 items-center gap-2">
            <span className={`inline-block shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${status.badgeClass}`}>
              {status.label}
            </span>
            {selected && !notebook.sync_enabled && (
              <span className="min-w-0 truncate text-xs font-medium text-warn">
                Not synced - will not return results until sync is enabled.
              </span>
            )}
          </div>
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
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => onSync(notebook.id)}
          disabled={syncDisabled}
          title={notebook.sync_enabled ? "Sync this notebook's pages from OneNote now" : 'Enable sync to sync this notebook'}
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
