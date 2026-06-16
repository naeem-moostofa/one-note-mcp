import { env } from '@/config/env'

// Kicks off the backend-owned Microsoft OAuth flow. Lives in the shared layer so
// any feature (landing sign-in, account connect/reconnect) can start login without
// importing another feature. Used for first-time connect AND reconnect — they're
// the same redirect.
export function beginMicrosoftLogin(): void {
  // Full-page navigation, not fetch — the backend immediately 302-redirects to
  // Microsoft, and OAuth redirects can't be followed by an XHR/fetch call.
  window.location.assign(`${env.API_BASE_URL}/auth/microsoft/login`)
}
