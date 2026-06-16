import { Navigate } from 'react-router-dom'

import { SignInButton } from '@/features/auth/components/sign-in-button'
import { useAuth } from '@/features/auth/hooks/use-auth'

// Each MCP client we can connect to. `accent` tints the placeholder so the three
// cards read as distinct; drop a real screenshot into /public/clients/<image> and
// swap the placeholder block for an <img> when you have them.
const MCP_CLIENTS = [
  {
    name: 'Cursor',
    monogram: 'C',
    accent: 'from-indigo-100 to-indigo-50',
    blurb: 'Add the MCP server in Cursor and search your notes without leaving the editor.',
  },
  {
    name: 'Claude Code',
    monogram: 'CC',
    accent: 'from-orange-100 to-amber-50',
    blurb: 'Connect from the terminal and pull notebook context into any coding session.',
  },
  {
    name: 'Codex',
    monogram: 'Cx',
    accent: 'from-sky-100 to-cyan-50',
    blurb: 'Wire it up as an MCP server and reference your handwritten notes on demand.',
  },
]

const STEPS = [
  'Sign in with Microsoft and connect your OneNote account.',
  'Pick which notebooks sync — handwriting is OCR’d and made searchable.',
  'Connect an MCP client and search your notes from your AI tools.',
]

export function LandingPage() {
  const { isAuthenticated } = useAuth()
  if (isAuthenticated) {
    return <Navigate to="/dashboard" replace />
  }

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-20 border-b border-line bg-canvas/80 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <span className="font-semibold text-ink">OneNote MCP</span>
          <SignInButton />
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6">
        {/* Hero */}
        <section className="py-16 text-center sm:py-24">
          <span className="inline-block rounded-full bg-brand-soft px-3 py-1 text-sm font-medium text-brand">
            Read-only · your notes stay in OneNote
          </span>
          <h1 className="mx-auto mt-6 max-w-3xl text-4xl font-bold tracking-tight text-ink sm:text-5xl">
            Search your OneNote notebooks — handwriting included — from your AI tools
          </h1>
          <p className="mx-auto mt-5 max-w-2xl text-lg text-muted">
            OneNote MCP syncs your notebooks, runs OCR over handwritten pages, and exposes them to
            MCP clients like Cursor, Claude Code, and Codex as fast, read-only search.
          </p>
        </section>

        {/* MCP clients */}
        <section className="py-12">
          <h2 className="text-center text-2xl font-semibold text-ink">Connect from your favorite tools</h2>
          <p className="mx-auto mt-2 max-w-xl text-center text-muted">
            One MCP connection, available wherever you already work.
          </p>
          <div className="mt-10 grid gap-6 sm:grid-cols-3">
            {MCP_CLIENTS.map((client) => (
              <ClientCard key={client.name} {...client} />
            ))}
          </div>
        </section>

        {/* How it works */}
        <section className="py-12">
          <h2 className="text-center text-2xl font-semibold text-ink">How it works</h2>
          <ol className="mx-auto mt-10 grid max-w-4xl gap-6 sm:grid-cols-3">
            {STEPS.map((step, index) => (
              <li key={step} className="rounded-xl border border-line bg-surface p-6">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand font-semibold text-white">
                  {index + 1}
                </div>
                <p className="mt-4 text-ink">{step}</p>
              </li>
            ))}
          </ol>
        </section>

        <footer className="border-t border-line py-10 text-center text-sm text-muted">
          OneNote MCP · read-only access to your OneNote
        </footer>
      </main>
    </div>
  )
}

function ClientCard({
  name,
  monogram,
  accent,
  blurb,
}: {
  name: string
  monogram: string
  accent: string
  blurb: string
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface">
      {/* Screenshot slot — replace this block with <img src="/clients/…" /> later. */}
      <div className={`flex aspect-video items-center justify-center bg-gradient-to-br ${accent}`}>
        <div className="flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-surface font-bold text-ink shadow-sm">
            {monogram}
          </div>
          <span className="text-xs font-medium text-muted">Screenshot coming soon</span>
        </div>
      </div>
      <div className="p-5">
        <h3 className="font-semibold text-ink">{name}</h3>
        <p className="mt-1 text-sm text-muted">{blurb}</p>
      </div>
    </div>
  )
}
