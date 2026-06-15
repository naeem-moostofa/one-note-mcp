import { QueryClientProvider } from '@tanstack/react-query'
import type { PropsWithChildren } from 'react'

import { AuthProvider } from '@/features/auth/auth-provider'
import { queryClient } from '@/lib/react-query'

// Composes the app-wide providers. Order: server-state cache outermost, then the
// auth session, then (via children) the router.
export function AppProvider({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  )
}
