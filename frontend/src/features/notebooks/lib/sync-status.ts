import type { NotebookWebResponse } from '@/types/api'

export interface SyncStatusDescriptor {
  label: string
  // Tailwind classes for the status badge, pulled from the palette tokens.
  badgeClass: string
}

// Maps a notebook's (sync_enabled, sync_status) pair to a human label + badge
// styling. `sync_enabled` wins: a disabled notebook reads "Disabled" regardless of
// its last status (the two axes are orthogonal — see the backend render contract).
export function describeSyncStatus(notebook: NotebookWebResponse): SyncStatusDescriptor {
  if (!notebook.sync_enabled) {
    return { label: 'Disabled', badgeClass: 'bg-line text-muted' }
  }
  switch (notebook.sync_status) {
    case 'FRESH':
      return { label: labelWithDate('Synced', notebook.last_synced_at), badgeClass: 'bg-ok-soft text-ok' }
    case 'SYNCING':
      return { label: 'Syncing…', badgeClass: 'bg-busy-soft text-busy' }
    case 'FAILED':
      return { label: 'Sync failed', badgeClass: 'bg-warn-soft text-warn' }
    case 'PENDING':
    default:
      return { label: 'Not synced yet', badgeClass: 'bg-brand-soft text-brand' }
  }
}

function labelWithDate(prefix: string, isoDate: string | null): string {
  if (!isoDate) {
    return prefix
  }
  return `${prefix} · ${new Date(isoDate).toLocaleDateString()}`
}
