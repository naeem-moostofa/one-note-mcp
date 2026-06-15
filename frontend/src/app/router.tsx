import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { HomePage } from '@/app/routes/home'
import { LoginPage } from '@/app/routes/login'
import { ProtectedRoute } from '@/features/auth/components/protected-route'

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route path="/" element={<HomePage />} />
        </Route>
        {/* Unknown paths fall back to the protected root (which itself redirects
            to /login when unauthenticated). */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
