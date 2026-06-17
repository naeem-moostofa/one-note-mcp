import { useMemo, useState, type FormEvent } from 'react'

import { Toggle } from '@/components/ui/toggle'
import { useCreateMCPConnection } from '@/features/mcp-keys/api/use-create-mcp-connection'
import type { MCPConnectionCreated } from '@/types/api'

export interface SelectedNotebook {
  id: number
  display_name: string
  sync_enabled: boolean
}

interface CreateKeyBarProps {
  allNotebooks: boolean
  selectedNotebooks: SelectedNotebook[]
  onAllNotebooksChange: (next: boolean) => void
  onClearSelection: () => void
  onCreated: (connection: MCPConnectionCreated, scope: SelectedNotebook[]) => void
}

export function CreateKeyBar({
  allNotebooks,
  selectedNotebooks,
  onAllNotebooksChange,
  onClearSelection,
  onCreated,
}: CreateKeyBarProps) {
  const [displayName, setDisplayName] = useState('')
  const createConnection = useCreateMCPConnection()
  const selectedCount = selectedNotebooks.length
  const canCreate = allNotebooks || selectedCount > 0
  const disabledSelectionCount = useMemo(
    () => selectedNotebooks.filter((notebook) => !notebook.sync_enabled).length,
    [selectedNotebooks],
  )

  const label = allNotebooks
    ? 'Create key - all notebooks'
    : selectedCount === 1
      ? 'Create key - 1 notebook'
      : `Create key - ${selectedCount} notebooks`

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canCreate || createConnection.isPending) {
      return
    }

    const trimmedName = displayName.trim()
    createConnection.mutate(
      {
        ...(trimmedName ? { display_name: trimmedName } : {}),
        scope_all_notebooks: allNotebooks,
        ...(allNotebooks ? {} : { notebook_ids: selectedNotebooks.map((notebook) => notebook.id) }),
      },
      {
        onSuccess: (connection) => {
          setDisplayName('')
          onCreated(connection, allNotebooks ? [] : selectedNotebooks)
        },
      },
    )
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="sticky top-3 z-20 rounded-xl border border-line bg-surface px-4 py-3 shadow-sm"
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
        <div className="flex flex-1 flex-col gap-1">
          <label htmlFor="mcp-key-name" className="text-sm font-medium text-ink">
            Key name
          </label>
          <input
            id="mcp-key-name"
            type="text"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="Optional"
            className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none transition-colors placeholder:text-muted focus:border-brand focus:bg-surface focus:ring-2 focus:ring-brand-soft"
          />
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-2 text-sm font-medium text-ink">
            <Toggle checked={allNotebooks} onChange={onAllNotebooksChange} label="Scope key to all notebooks" />
            All notebooks
          </label>

          <button
            type="button"
            onClick={onClearSelection}
            disabled={!allNotebooks && selectedCount === 0}
            className="rounded-lg px-3 py-2 text-sm font-medium text-muted transition-colors hover:bg-brand-soft hover:text-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            Clear selection
          </button>

          {canCreate ? (
            <button
              type="submit"
              disabled={createConnection.isPending}
              className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {createConnection.isPending ? 'Creating...' : label}
            </button>
          ) : (
            <p className="px-1 text-sm text-muted">Select notebooks to create a key.</p>
          )}
        </div>
      </div>

      {(canCreate || createConnection.isError) && (
        <div className="mt-3 flex flex-col gap-1 text-sm sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted">
            {allNotebooks
              ? 'This key will include notebooks you add later.'
              : `${selectedCount} selected.`}
          </p>
          {disabledSelectionCount > 0 && !allNotebooks && (
            <p className="text-warn">
              {disabledSelectionCount} not synced until you enable sync.
            </p>
          )}
          {createConnection.isError && (
            <p className="text-warn">Could not create the key. Check the selection and try again.</p>
          )}
        </div>
      )}
    </form>
  )
}
