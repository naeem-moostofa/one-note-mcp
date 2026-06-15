import { useAuth } from '@/features/auth/hooks/use-auth'

// The authenticated landing surface. Intentionally blank for now — real pages
// (Notebooks, MCP connections, Account) get built in later stages. A small
// sign-out affordance is kept so the auth flow is testable end-to-end.
export function HomePage() {
  const { signOut } = useAuth()
  return (
    <main style={{ minHeight: '100vh' }}>
      <button type="button" onClick={signOut} style={{ position: 'fixed', top: 12, right: 12 }}>
        Sign out
      </button>
    </main>
  )
}
