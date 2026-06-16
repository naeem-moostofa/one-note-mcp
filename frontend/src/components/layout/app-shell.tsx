import type { ReactNode } from 'react'

interface AppShellProps {
  onSignOut: () => void
  children: ReactNode
}

// Standard "stacked" app-shell layout: the top bar's background/border is
// full-bleed, but its contents and the page content share one centered max-width
// container (max-w-7xl) so they line up and don't stretch on wide monitors.
export function AppShell({ onSignOut, children }: AppShellProps) {
  return (
    <div className="min-h-full">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex w-full max-w-7xl items-center justify-between px-6 py-3">
          <span className="font-semibold text-ink">OneNote MCP</span>
          <button
            type="button"
            onClick={onSignOut}
            className="rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-muted transition-colors hover:bg-canvas hover:text-ink"
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl px-6 py-8">{children}</main>
    </div>
  )
}
