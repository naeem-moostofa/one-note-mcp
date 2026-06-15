import { Navigate } from 'react-router-dom'

import { SignInButton } from '@/features/auth/components/sign-in-button'
import { useAuth } from '@/features/auth/hooks/use-auth'

export function LoginPage() {
  const { isAuthenticated } = useAuth()
  if (isAuthenticated) {
    return <Navigate to="/" replace />
  }
  return (
    <main style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
      <SignInButton />
    </main>
  )
}
