import { env } from '@/config/env'

export function SignInButton() {
  const handleSignIn = () => {
    // Full-page navigation, not fetch — the backend immediately 302-redirects to
    // Microsoft, and OAuth redirects can't be followed by an XHR/fetch call.
    window.location.assign(`${env.API_BASE_URL}/auth/microsoft/login`)
  }

  return (
    <button type="button" onClick={handleSignIn}>
      Sign in with Microsoft
    </button>
  )
}
