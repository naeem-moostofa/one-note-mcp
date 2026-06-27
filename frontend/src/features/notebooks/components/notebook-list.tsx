import { useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { parseAsInteger, parseAsString, parseAsStringEnum, useQueryStates } from 'nuqs'

import { useNotebooks } from '@/features/notebooks/api/use-notebooks'
import { useRefreshNotebooks } from '@/features/notebooks/api/use-refresh-notebooks'
import { useSyncNotebook } from '@/features/notebooks/api/use-sync-notebook'
import { useToggleSync } from '@/features/notebooks/api/use-toggle-sync'
import { CreateKeyBar, type SelectedNotebook } from '@/features/mcp-keys/components/create-key-bar'
import { KeyRevealModal } from '@/features/mcp-keys/components/key-reveal-modal'
import { NotebookCard } from '@/features/notebooks/components/notebook-card'
import { beginMicrosoftLogin } from '@/lib/microsoft-login'
import type { MCPConnectionCreated, MicrosoftConnectionStatus, NotebookFilter, NotebookSyncStatus, NotebookWebResponse } from '@/types/api'

type SyncEnabledFilter = 'enabled' | 'disabled'

// Fixed page size — the backend clamps limit to [1, 100]; 50 matches its default.
const PAGE_SIZE = 50

const syncStatusValues: NotebookSyncStatus[] = ['PENDING', 'SYNCING', 'FRESH', 'FAILED']
const EMPTY_NOTEBOOKS: NotebookWebResponse[] = []
const filterParsers = {
  search: parseAsString.withDefault(''),
  syncEnabled: parseAsStringEnum<SyncEnabledFilter>(['enabled', 'disabled']),
  syncStatus: parseAsStringEnum<NotebookSyncStatus>(syncStatusValues),
  offset: parseAsInteger.withDefault(0),
}

interface NotebookListProps {
  // From GET /api/me — gates the "Refresh from OneNote" action (which needs an
  // active Microsoft connection) and drives the connect prompt.
  microsoftStatus: MicrosoftConnectionStatus | null | undefined
}

export function NotebookList({ microsoftStatus }: NotebookListProps) {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [selectedNotebooks, setSelectedNotebooks] = useState<Map<number, SelectedNotebook>>(() => new Map())
  const [allNotebooks, setAllNotebooks] = useState(false)
  const [revealedKey, setRevealedKey] = useState<MCPConnectionCreated | null>(null)
  const [revealedScope, setRevealedScope] = useState<SelectedNotebook[]>([])
  const searchDebounceTimeout = useRef<number | null>(null)
  const searchInputRef = useRef<HTMLInputElement | null>(null)
  const [urlFilters, setUrlFilters] = useQueryStates(filterParsers, {
    urlKeys: {
      search: 'search',
      syncEnabled: 'sync_enabled',
      syncStatus: 'sync_status',
      offset: 'offset',
    },
  })
  // Content filters only (drive chips + empty state); pagination is tracked separately.
  const filters = useMemo(() => {
    const trimmedSearch = urlFilters.search.trim()
    const nextFilters: NotebookFilter = {}
    if (trimmedSearch) {
      nextFilters.search = trimmedSearch
    }
    if (urlFilters.syncEnabled !== null) {
      nextFilters.sync_enabled = urlFilters.syncEnabled === 'enabled'
    }
    if (urlFilters.syncStatus !== null) {
      nextFilters.sync_status = urlFilters.syncStatus
    }
    return nextFilters
  }, [urlFilters])
  // What's actually sent to the API: content filters + the current page window.
  const apiFilters = useMemo<NotebookFilter>(
    () => ({ ...filters, limit: PAGE_SIZE, offset: urlFilters.offset }),
    [filters, urlFilters.offset],
  )
  const { data: page, isPending, isError, refetch } = useNotebooks(apiFilters)
  const notebooks = page?.data ?? EMPTY_NOTEBOOKS
  const total = page?.total ?? 0
  const offset = urlFilters.offset
  const toggleSync = useToggleSync()
  const syncNotebook = useSyncNotebook()
  const refresh = useRefreshNotebooks()

  const connected = microsoftStatus === 'ACTIVE'
  const hasFilters = Object.keys(filters).length > 0
  const activeFilterCount = Object.keys(filters).length
  const currentNotebookById = useMemo(() => new Map(notebooks.map((notebook) => [notebook.id, notebook])), [notebooks])
  const selectedNotebookList = useMemo(() => {
    return Array.from(selectedNotebooks.values())
      .map((selected) => {
        const current = currentNotebookById.get(selected.id)
        return current ? { ...selected, sync_enabled: current.sync_enabled } : selected
      })
      .sort((left, right) => left.display_name.localeCompare(right.display_name))
  }, [currentNotebookById, selectedNotebooks])

  useEffect(() => {
    return () => {
      if (searchDebounceTimeout.current !== null) {
        window.clearTimeout(searchDebounceTimeout.current)
      }
    }
  }, [])

  useEffect(() => {
    if (searchInputRef.current !== null && document.activeElement !== searchInputRef.current) {
      searchInputRef.current.value = urlFilters.search
    }
  }, [urlFilters.search])

  function clearSearchDebounce() {
    if (searchDebounceTimeout.current !== null) {
      window.clearTimeout(searchDebounceTimeout.current)
      searchDebounceTimeout.current = null
    }
  }

  function handleSearchInputChange(event: ChangeEvent<HTMLInputElement>) {
    const nextSearch = event.target.value
    clearSearchDebounce()
    searchDebounceTimeout.current = window.setTimeout(() => {
      // Reset to the first page — a narrowed result set can't keep a stale offset.
      const trimmedSearch = nextSearch.trim()
      void setUrlFilters({ search: trimmedSearch || null, offset: 0 })
    }, 300)
  }

  function setSearchInputDomValue(value: string) {
    if (searchInputRef.current !== null) {
      searchInputRef.current.value = value
    }
  }

  function clearSearchFilter() {
    clearSearchDebounce()
    setSearchInputDomValue('')
    void setUrlFilters({ search: null, offset: 0 })
  }

  function clearAllFilters() {
    clearSearchDebounce()
    setSearchInputDomValue('')
    void setUrlFilters(null)
  }

  function handleSelectNotebook(notebook: NotebookWebResponse, selected: boolean) {
    setSelectedNotebooks((current) => {
      const next = new Map(current)
      if (selected) {
        next.set(notebook.id, {
          id: notebook.id,
          display_name: notebook.display_name,
          sync_enabled: notebook.sync_enabled,
        })
      } else {
        next.delete(notebook.id)
      }
      return next
    })
  }

  function handleAllNotebooksChange(next: boolean) {
    setAllNotebooks(next)
    if (next) {
      setSelectedNotebooks(new Map())
    }
  }

  function clearKeySelection() {
    setAllNotebooks(false)
    setSelectedNotebooks(new Map())
  }

  function handleKeyCreated(connection: MCPConnectionCreated, scope: SelectedNotebook[]) {
    setRevealedKey(connection)
    setRevealedScope(scope)
  }

  function closeRevealModal() {
    setRevealedKey(null)
    setRevealedScope([])
    clearKeySelection()
  }

  return (
    <section className="flex flex-col gap-5">
      <header className="flex items-center justify-between gap-4 rounded-xl border border-brand-soft bg-brand-soft/60 px-5 py-4">
        <div>
          <h2 className="text-lg font-semibold text-ink">Notebooks</h2>
          <p className="text-sm text-muted">
            Refresh pulls your latest notebook list; Sync pulls a notebook’s pages so they’re searchable from your MCP clients.
          </p>
        </div>
        <button
          type="button"
          onClick={() => refresh.mutate()}
          disabled={!connected || refresh.isPending}
          title={connected ? 'Pull the latest notebook list from OneNote (names only)' : 'Connect Microsoft to refresh'}
          className="shrink-0 rounded-lg border border-brand bg-surface px-3 py-2 text-sm font-medium text-brand transition-colors hover:bg-brand hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {refresh.isPending ? 'Refreshing…' : 'Refresh list'}
        </button>
      </header>

      <div className="flex flex-col gap-4 rounded-xl border border-line bg-surface px-5 py-4 shadow-sm">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="min-w-0 flex-1">
            <label htmlFor="notebook-search" className="mb-2 block text-sm font-medium text-ink">
              Search notebooks
            </label>
            <input
              id="notebook-search"
              ref={searchInputRef}
              type="search"
              defaultValue={urlFilters.search}
              onChange={handleSearchInputChange}
              placeholder="Search by notebook name"
              className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none transition-colors placeholder:text-muted focus:border-brand focus:bg-surface focus:ring-2 focus:ring-brand-soft"
            />
          </div>
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            className="rounded-lg border border-brand bg-surface px-3 py-2 text-sm font-medium text-brand transition-colors hover:bg-brand hover:text-white"
          >
            Filters{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}
          </button>
        </div>

        {hasFilters && (
          <div className="flex flex-wrap gap-2">
            {filters.search && (
              <ActiveFilterChip label={`Name: ${filters.search}`} onRemove={clearSearchFilter} />
            )}
            {urlFilters.syncEnabled && (
              <ActiveFilterChip
                label={urlFilters.syncEnabled === 'enabled' ? 'Enabled' : 'Disabled'}
                onRemove={() => void setUrlFilters({ syncEnabled: null, offset: 0 })}
              />
            )}
            {urlFilters.syncStatus && (
              <ActiveFilterChip
                label={`Status: ${describeStatusFilter(urlFilters.syncStatus)}`}
                onRemove={() => void setUrlFilters({ syncStatus: null, offset: 0 })}
              />
            )}
            <button
              type="button"
              onClick={clearAllFilters}
              className="rounded-full px-3 py-1 text-xs font-medium text-muted transition-colors hover:bg-brand-soft hover:text-brand"
            >
              Clear all
            </button>
          </div>
        )}
      </div>

      <FilterDrawer
        open={drawerOpen}
        syncEnabled={urlFilters.syncEnabled}
        syncStatus={urlFilters.syncStatus}
        onClose={() => setDrawerOpen(false)}
        onSyncEnabledChange={(value) => void setUrlFilters({ syncEnabled: value, offset: 0 })}
        onSyncStatusChange={(value) => void setUrlFilters({ syncStatus: value, offset: 0 })}
        onClear={() => void setUrlFilters({ syncEnabled: null, syncStatus: null, offset: 0 })}
      />

      {connected && (notebooks.length > 0 || selectedNotebookList.length > 0 || allNotebooks) && (
        <CreateKeyBar
          allNotebooks={allNotebooks}
          selectedNotebooks={selectedNotebookList}
          onAllNotebooksChange={handleAllNotebooksChange}
          onClearSelection={clearKeySelection}
          onCreated={handleKeyCreated}
        />
      )}

      {refresh.isError && (
        <p className="rounded-lg bg-warn-soft px-4 py-2 text-sm text-warn">
          Couldn’t refresh — make sure your Microsoft account is connected and try again.
        </p>
      )}

      {isPending ? (
        <ListSkeleton />
      ) : isError ? (
        <EmptyCard>
          <p className="text-muted">Couldn’t load your notebooks.</p>
          <PrimaryButton onClick={() => void refetch()}>Try again</PrimaryButton>
        </EmptyCard>
      ) : notebooks.length === 0 ? (
        <EmptyCard>
          {hasFilters ? (
            <p className="text-muted">No notebooks match your filters.</p>
          ) : connected ? (
            <>
              <p className="text-muted">No notebooks yet. Refresh the list from OneNote to get started.</p>
              <PrimaryButton onClick={() => refresh.mutate()} disabled={refresh.isPending}>
                {refresh.isPending ? 'Refreshing…' : 'Refresh list'}
              </PrimaryButton>
            </>
          ) : (
            <>
              <p className="text-muted">Connect your Microsoft account to load your notebooks.</p>
              <PrimaryButton onClick={beginMicrosoftLogin}>Connect Microsoft</PrimaryButton>
            </>
          )}
        </EmptyCard>
      ) : (
        <div className="flex flex-col gap-3">
          {notebooks.map((notebook) => (
            <NotebookCard
              key={notebook.id}
              notebook={notebook}
              onToggle={(id, syncEnabled) => toggleSync.mutate({ id, syncEnabled })}
              onSync={(id) => syncNotebook.mutate(id)}
              disabled={toggleSync.pendingNotebookIds.has(notebook.id)}
              selected={selectedNotebooks.has(notebook.id)}
              onSelectChange={handleSelectNotebook}
              selectionDisabled={allNotebooks}
            />
          ))}
          <PaginationControls
            total={total}
            offset={offset}
            count={notebooks.length}
            onPrev={() => void setUrlFilters({ offset: Math.max(0, offset - PAGE_SIZE) })}
            onNext={() => void setUrlFilters({ offset: offset + PAGE_SIZE })}
          />
        </div>
      )}

      {revealedKey && (
        <KeyRevealModal connection={revealedKey} scopedNotebooks={revealedScope} onDone={closeRevealModal} />
      )}
    </section>
  )
}

function PaginationControls({
  total,
  offset,
  count,
  onPrev,
  onNext,
}: {
  total: number
  offset: number
  count: number
  onPrev: () => void
  onNext: () => void
}) {
  // Only meaningful once there's more than one page of results.
  if (total <= count && offset === 0) {
    return null
  }
  const start = total === 0 ? 0 : offset + 1
  const end = offset + count
  const canPrev = offset > 0
  const canNext = end < total

  return (
    <div className="flex items-center justify-between gap-4 pt-1">
      <p className="text-sm text-muted">
        Showing {start}–{end} of {total}
      </p>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onPrev}
          disabled={!canPrev}
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-50"
        >
          Previous
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canNext}
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {[0, 1, 2].map((index) => (
        <div key={index} className="h-[68px] animate-pulse rounded-xl border border-line bg-surface" />
      ))}
    </div>
  )
}

function EmptyCard({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-4 rounded-xl border border-dashed border-line bg-surface px-6 py-12 text-center">
      {children}
    </div>
  )
}

function ActiveFilterChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      className="rounded-full border border-brand-soft bg-brand-soft px-3 py-1.5 text-xs font-medium text-brand transition-colors hover:border-brand"
      title={`Remove ${label} filter`}
    >
      {label} x
    </button>
  )
}

function FilterDrawer({
  open,
  syncEnabled,
  syncStatus,
  onClose,
  onSyncEnabledChange,
  onSyncStatusChange,
  onClear,
}: {
  open: boolean
  syncEnabled: SyncEnabledFilter | null
  syncStatus: NotebookSyncStatus | null
  onClose: () => void
  onSyncEnabledChange: (value: SyncEnabledFilter | null) => void
  onSyncStatusChange: (value: NotebookSyncStatus | null) => void
  onClear: () => void
}) {
  if (!open) {
    return null
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink/30" role="presentation" onClick={onClose}>
      <aside
        aria-label="Notebook filters"
        className="h-full w-full max-w-md bg-surface shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex h-full flex-col">
          <header className="flex items-center justify-between border-b border-line px-6 py-5">
            <h3 className="text-lg font-semibold text-ink">Filters</h3>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-2 py-1 text-sm font-medium text-muted transition-colors hover:bg-brand-soft hover:text-brand"
            >
              Close
            </button>
          </header>

          <div className="flex flex-1 flex-col gap-8 overflow-y-auto px-6 py-6">
            <fieldset>
              <legend className="mb-4 text-sm font-semibold text-ink">Sync setting</legend>
              <div className="flex flex-col gap-2.5">
              <CheckboxFilter
                label="Enabled"
                checked={syncEnabled === 'enabled'}
                onChange={(checked) => onSyncEnabledChange(checked ? 'enabled' : null)}
              />
              <CheckboxFilter
                label="Disabled"
                checked={syncEnabled === 'disabled'}
                onChange={(checked) => onSyncEnabledChange(checked ? 'disabled' : null)}
              />
              </div>
            </fieldset>

            <fieldset>
              <legend className="mb-4 text-sm font-semibold text-ink">Status</legend>
              <div className="flex flex-col gap-2.5">
              {syncStatusValues.map((status) => (
                <CheckboxFilter
                  key={status}
                  label={describeStatusFilter(status)}
                  checked={syncStatus === status}
                  onChange={(checked) => onSyncStatusChange(checked ? status : null)}
                />
              ))}
              </div>
            </fieldset>
          </div>

          <footer className="flex items-center justify-between border-t border-line px-6 py-5">
            <button
              type="button"
              onClick={onClear}
              className="rounded-lg px-3 py-2 text-sm font-medium text-muted transition-colors hover:bg-brand-soft hover:text-brand"
            >
              Clear filters
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-hover"
            >
              Apply
            </button>
          </footer>
        </div>
      </aside>
    </div>
  )
}

function CheckboxFilter({
  checked,
  label,
  onChange,
}: {
  checked: boolean
  label: string
  onChange: (checked: boolean) => void
}) {
  return (
    <label className="flex items-center gap-3 rounded-lg border border-line bg-canvas px-3.5 py-3 text-sm text-ink transition-colors hover:border-brand-soft hover:bg-brand-soft/50">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 accent-brand"
      />
      {label}
    </label>
  )
}

function describeStatusFilter(status: NotebookSyncStatus) {
  switch (status) {
    case 'PENDING':
      return 'Pending'
    case 'SYNCING':
      return 'Syncing'
    case 'FRESH':
      return 'Synced'
    case 'FAILED':
      return 'Failed'
  }
}

function PrimaryButton({
  children,
  onClick,
  disabled,
}: {
  children: ReactNode
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  )
}
