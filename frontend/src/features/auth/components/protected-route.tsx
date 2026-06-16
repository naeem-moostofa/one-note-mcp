import { Navigate, Outlet } from 'react-router-dom'

import { useAuth } from '@/features/auth/hooks/use-auth'

// Gate for authenticated pages: renders the matched child route if a session
// exists, otherwise redirects to the login screen.
export function ProtectedRoute() {
  const { isAuthenticated } = useAuth()
  if (!isAuthenticated) {
    return <Navigate to="/" replace />
  }
  return <Outlet />
}
