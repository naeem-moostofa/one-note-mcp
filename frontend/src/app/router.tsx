import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { DashboardPage } from '@/app/routes/dashboard'
import { LandingPage } from '@/app/routes/landing'
import { ProtectedRoute } from '@/features/auth/components/protected-route'

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route element={<ProtectedRoute />}>
          <Route path="/dashboard" element={<DashboardPage />} />
        </Route>
        {/* Unknown paths fall back to the landing page (which forwards signed-in
            users on to /dashboard). */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
