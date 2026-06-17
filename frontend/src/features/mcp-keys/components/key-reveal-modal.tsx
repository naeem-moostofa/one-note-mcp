import { useMemo, useState } from 'react'

import type { SelectedNotebook } from '@/features/mcp-keys/components/create-key-bar'
import type { MCPConnectionCreated } from '@/types/api'

type ClientTab = 'cursor' | 'claude' | 'codex' | 'other'

// The MCP server name written into each client's config / deeplink. It's a
// purely client-side label that keys the server entry in the user's config, so
// it must be unique per key — otherwise installing a second key would overwrite
// the first. Derive it from the key's name (falling back to its id).
function deriveServerName(connection: MCPConnectionCreated) {
  const slug = (connection.display_name ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '')
  return slug ? `onenote-${slug}` : `onenote-${connection.id}`
}

interface KeyRevealModalProps {
  connection: MCPConnectionCreated
  scopedNotebooks: SelectedNotebook[]
  onDone: () => void
}

export function KeyRevealModal({ connection, scopedNotebooks, onDone }: KeyRevealModalProps) {
  const [activeTab, setActiveTab] = useState<ClientTab>('cursor')
  const [copied, setCopied] = useState<string | null>(null)
  const [copyFailed, setCopyFailed] = useState<string | null>(null)
  const serverName = useMemo(() => deriveServerName(connection), [connection])
  const setup = useMemo(
    () => buildClientSetup(activeTab, serverName, connection.mcp_url, connection.raw_token),
    [activeTab, serverName, connection.mcp_url, connection.raw_token],
  )
  const unsyncedCount = scopedNotebooks.filter((notebook) => !notebook.sync_enabled).length

  async function copyText(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopyFailed(null)
      setCopied(label)
      window.setTimeout(() => setCopied(null), 2000)
    } catch {
      setCopied(null)
      setCopyFailed(label)
      window.setTimeout(() => setCopyFailed(null), 4000)
    }
  }

  function copyLabel(label: string, idle: string) {
    if (copyFailed === label) {
      return 'Copy failed — select manually'
    }
    return copied === label ? 'Copied' : idle
  }

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-ink/40 px-4 py-6 backdrop-blur-sm" role="presentation">
      <div
        className="mx-auto flex max-h-[calc(100vh-3rem)] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-line bg-canvas shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mcp-key-title"
      >
        <header className="flex flex-col gap-4 border-b border-line bg-canvas px-5 py-5 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-sm font-semibold text-brand">MCP key created</p>
            <h2 id="mcp-key-title" className="mt-1 text-2xl font-semibold text-ink">
              Copy this key now
            </h2>
            <p className="mt-2 max-w-2xl text-sm text-muted">
              The raw token is only shown once. If you lose it, create another key.
            </p>
          </div>
          <button
            type="button"
            onClick={onDone}
            className="self-start rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-hover"
          >
            Done
          </button>
        </header>

        <main className="flex flex-1 flex-col gap-5 overflow-y-auto px-5 py-5">
          <section className="rounded-xl border border-warn bg-warn-soft px-4 py-3">
            <p className="text-sm font-medium text-warn">One-time secret</p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              <input
                type="text"
                readOnly
                value={connection.raw_token}
                className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 py-2 font-mono text-sm text-ink"
                aria-label="MCP API key"
              />
              <button
                type="button"
                onClick={() => void copyText('token', connection.raw_token)}
                className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-hover"
              >
                {copyLabel('token', 'Copy key')}
              </button>
            </div>
          </section>

          <section className="rounded-xl border border-line bg-surface px-4 py-4">
            <h3 className="text-base font-semibold text-ink">Notebook scope</h3>
            {connection.scope_all_notebooks ? (
              <p className="mt-2 text-sm text-muted">All notebooks, including notebooks added later.</p>
            ) : (
              <div className="mt-3 flex flex-col gap-3">
                <p className="text-sm text-muted">
                  {scopedNotebooks.length === 1 ? '1 notebook selected' : `${scopedNotebooks.length} notebooks selected`}
                </p>
                <div className="grid max-h-44 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
                  {scopedNotebooks.map((notebook) => (
                    <div
                      key={notebook.id}
                      className="flex min-w-0 flex-col gap-1 rounded-lg border border-line bg-canvas px-3 py-2"
                    >
                      <span className="min-w-0 truncate text-sm font-medium text-ink" title={notebook.display_name}>
                        {notebook.display_name}
                      </span>
                      {!notebook.sync_enabled && (
                        <span className="text-xs font-medium text-warn">Not synced until sync is enabled</span>
                      )}
                    </div>
                  ))}
                </div>
                {unsyncedCount > 0 && (
                  <p className="text-sm text-warn">
                    Unsynced notebooks are in scope, but MCP searches return results only after sync is enabled.
                  </p>
                )}
              </div>
            )}
          </section>

          <section className="rounded-xl border border-line bg-surface px-4 py-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <h3 className="text-base font-semibold text-ink">Client setup</h3>
              <div className="flex rounded-lg border border-line bg-canvas p-1">
                <TabButton label="Cursor" active={activeTab === 'cursor'} onClick={() => setActiveTab('cursor')} />
                <TabButton label="Claude Code" active={activeTab === 'claude'} onClick={() => setActiveTab('claude')} />
                <TabButton label="Codex" active={activeTab === 'codex'} onClick={() => setActiveTab('codex')} />
                <TabButton label="Other" active={activeTab === 'other'} onClick={() => setActiveTab('other')} />
              </div>
            </div>

            <div className="mt-4 flex flex-col gap-4">
              {setup.deeplink && (
                <div className="flex flex-col gap-2">
                  <p className="text-sm font-medium text-ink">One-click install</p>
                  <a
                    href={setup.deeplink}
                    className="inline-flex w-fit items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-hover"
                  >
                    Add to Cursor
                  </a>
                  <p className="text-xs text-muted">Opens Cursor and adds the server with this key.</p>
                </div>
              )}

              {setup.command && (
                <div className="flex flex-col gap-2">
                  <p className="text-sm font-medium text-ink">Run this command</p>
                  <pre className="overflow-auto rounded-lg bg-ink px-4 py-3 text-sm text-white">
                    <code>{setup.command}</code>
                  </pre>
                  <button
                    type="button"
                    onClick={() => void copyText('command', setup.command!)}
                    className="self-start rounded-lg border border-line px-3 py-2 text-sm font-medium text-ink transition-colors hover:bg-brand-soft"
                  >
                    {copyLabel('command', 'Copy command')}
                  </button>
                </div>
              )}

              <div className="flex flex-col gap-2">
                <p className="text-sm font-medium text-ink">
                  {setup.deeplink || setup.command ? `Or add to ${setup.snippetLabel} manually` : `Add to ${setup.snippetLabel}`}
                </p>
                <pre className="max-h-[420px] overflow-auto rounded-lg bg-ink px-4 py-3 text-sm text-white">
                  <code>{setup.snippet}</code>
                </pre>
                <button
                  type="button"
                  onClick={() => void copyText('snippet', setup.snippet)}
                  className="self-start rounded-lg border border-line px-3 py-2 text-sm font-medium text-ink transition-colors hover:bg-brand-soft"
                >
                  {copyLabel('snippet', 'Copy config')}
                </button>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  )
}

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
        active ? 'bg-surface text-brand shadow-sm' : 'text-muted hover:text-ink'
      }`}
    >
      {label}
    </button>
  )
}

interface ClientSetup {
  // The easiest install path for this client, if one embeds the token directly:
  deeplink?: string // Cursor: cursor:// one-click install
  command?: string // Claude Code: a single CLI command
  // The manual config block, always provided as a fallback.
  snippet: string
  snippetLabel: string
}

function buildClientSetup(tab: ClientTab, serverName: string, mcpUrl: string, rawToken: string): ClientSetup {
  const authHeader = `Bearer ${rawToken}`

  if (tab === 'cursor') {
    // The deeplink's `config` is the base64 of the inner server object (not the
    // { mcpServers } wrapper). base64 contains +,/,= so it's URL-encoded.
    const serverConfig = { url: mcpUrl, headers: { Authorization: authHeader } }
    const encodedConfig = encodeURIComponent(window.btoa(JSON.stringify(serverConfig)))
    return {
      deeplink: `cursor://anysphere.cursor-deeplink/mcp/install?name=${encodeURIComponent(serverName)}&config=${encodedConfig}`,
      snippet: JSON.stringify({ mcpServers: { [serverName]: serverConfig } }, null, 2),
      snippetLabel: 'mcp.json',
    }
  }

  if (tab === 'claude') {
    // `--transport http` registers a remote MCP server; --header carries the token.
    const command = `claude mcp add --transport http ${serverName} ${mcpUrl} --header "Authorization: ${authHeader}"`
    const snippet = JSON.stringify(
      { mcpServers: { [serverName]: { type: 'http', url: mcpUrl, headers: { Authorization: authHeader } } } },
      null,
      2,
    )
    return { command, snippet, snippetLabel: '.mcp.json' }
  }

  if (tab === 'codex') {
    // Codex: config.toml. The CLI's bearer flag only takes an env var, so the
    // copy-paste config (which embeds the token via http_headers) is the clean path.
    const snippet = [
      `[mcp_servers.${serverName}]`,
      `url = "${escapeTomlString(mcpUrl)}"`,
      `http_headers = { Authorization = "${escapeTomlString(authHeader)}" }`,
    ].join('\n')
    return { snippet, snippetLabel: '~/.codex/config.toml' }
  }

  const snippet = JSON.stringify(
    {
      mcpServers: {
        [serverName]: {
          type: 'http',
          url: mcpUrl,
          headers: { Authorization: authHeader },
        },
      },
    },
    null,
    2,
  )
  return { snippet, snippetLabel: 'a generic MCP client' }
}

function escapeTomlString(value: string) {
  return value.replaceAll('\\', '\\\\').replaceAll('"', '\\"')
}
