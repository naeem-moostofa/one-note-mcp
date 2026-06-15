import { useMemo, useState } from 'react'
import type { PropsWithChildren } from 'react'

import { AuthContext } from '@/features/auth/auth-context'
import type { AuthContextValue } from '@/features/auth/auth-context'
import { queryClient } from '@/lib/react-query'
import { clearStoredToken, getStoredToken, setStoredToken } from '@/lib/token-storage'

/**
 * The backend OAuth callback redirects the browser back to the SPA at
 * `{FRONTEND_ORIGIN}?token=<jwt>` (see backend `auth.py`). Pull that token off the
 * URL, persist it, and strip it from the address bar so it doesn't linger in
 * history/bookmarks. Returns the captured token, or null if there wasn't one.
 *
 * Idempotent: a second call (e.g. React StrictMode re-invoking the state
 * initializer in dev) finds no `token` param and returns null, leaving the
 * already-stored value untouched.
 */
function captureTokenFromUrl(): string | null {
  const params = new URLSearchParams(window.location.search)
  const token = params.get('token')
  if (!token) {
    return null
  }
  setStoredToken(token)
  params.delete('token')
  const query = params.toString()
  const cleanedUrl = window.location.pathname + (query ? `?${query}` : '') + window.location.hash
  window.history.replaceState({}, '', cleanedUrl)
  return token
}

export function AuthProvider({ children }: PropsWithChildren) {
  // Resolve the token synchronously before the first render so ProtectedRoute
  // doesn't briefly redirect to /login on a fresh post-login load.
  const [token, setToken] = useState<string | null>(() => captureTokenFromUrl() ?? getStoredToken())

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      isAuthenticated: token !== null,
      signOut: () => {
        clearStoredToken()
        setToken(null)
        // Drop all cached server state so the next session never sees the
        // previous user's data (e.g. a stale /api/me flashing before refetch).
        queryClient.clear()
      },
    }),
    [token],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
