// Low-level persistence for the app JWT. Lives in the shared layer (not the auth
// feature) because the api-client also needs to read it, and shared code must not
// import from features. The auth feature builds its context on top of this.
//
// localStorage (not an httpOnly cookie) matches the backend's Bearer-header auth
// model. The XSS tradeoff is accepted for the local V1.

const TOKEN_KEY = 'onenote_mcp_token'

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setStoredToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearStoredToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}
