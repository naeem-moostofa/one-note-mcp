import type { ReactNode } from 'react'

import { useNotebooks } from '@/features/notebooks/api/use-notebooks'
import { useRefreshNotebooks } from '@/features/notebooks/api/use-refresh-notebooks'
import { useSyncNotebook } from '@/features/notebooks/api/use-sync-notebook'
import { useToggleSync } from '@/features/notebooks/api/use-toggle-sync'
import { NotebookCard } from '@/features/notebooks/components/notebook-card'
import { beginMicrosoftLogin } from '@/lib/microsoft-login'
import type { MicrosoftConnectionStatus } from '@/types/api'

interface NotebookListProps {
  // From GET /api/me — gates the "Refresh from OneNote" action (which needs an
  // active Microsoft connection) and drives the connect prompt.
  microsoftStatus: MicrosoftConnectionStatus | null | undefined
}

export function NotebookList({ microsoftStatus }: NotebookListProps) {
  const { data: notebooks, isPending, isError, refetch } = useNotebooks()
  const toggleSync = useToggleSync()
  const syncNotebook = useSyncNotebook()
  const refresh = useRefreshNotebooks()

  const connected = microsoftStatus === 'ACTIVE'

  return (
    <section className="flex flex-col gap-5">
      <header className="flex items-center justify-between gap-4">
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
          className="shrink-0 rounded-lg border border-line bg-surface px-3 py-2 text-sm font-medium text-ink transition-colors hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-50"
        >
          {refresh.isPending ? 'Refreshing…' : 'Refresh list'}
        </button>
      </header>

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
          {connected ? (
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
            />
          ))}
        </div>
      )}
    </section>
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
