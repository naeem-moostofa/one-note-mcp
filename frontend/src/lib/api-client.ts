import axios from 'axios'

import { env } from '@/config/env'
import { clearStoredToken, getStoredToken } from '@/lib/token-storage'

// Single configured axios instance — the choke point for auth and error handling.
export const apiClient = axios.create({
  baseURL: env.API_BASE_URL,
})

// Request: attach the app JWT as a Bearer token when present.
apiClient.interceptors.request.use((config) => {
  const token = getStoredToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Response: 401 means the session is gone — clear it and bounce to login.
// This is THE place the "401 ⇒ re-authenticate" contract lives. A 403/404/400 is
// deliberately NOT handled here: those are surfaced inline by the calling code,
// since they don't mean the session is invalid.
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      clearStoredToken()
      if (window.location.pathname !== '/login') {
        window.location.assign('/login')
      }
    }
    return Promise.reject(error)
  },
)
