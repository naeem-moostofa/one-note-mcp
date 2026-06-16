import { AppShell } from '@/components/layout/app-shell'
import { useMe } from '@/features/account/api/use-me'
import { useAuth } from '@/features/auth/hooks/use-auth'
import { NotebookList } from '@/features/notebooks/components/notebook-list'

// The single signed-in page: the notebook dashboard inside the app shell. Wires the
// session (sign out) and profile (email, Microsoft status) into the presentational
// pieces.
export function DashboardPage() {
  const { signOut } = useAuth()
  const { data: me } = useMe()

  return (
    <AppShell onSignOut={signOut}>
      <NotebookList microsoftStatus={me?.microsoft_status} />
    </AppShell>
  )
}
