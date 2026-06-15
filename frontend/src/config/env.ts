// Typed, fail-fast access to the Vite environment. Importing this module throws
// immediately at startup if a required variable is missing, rather than letting
// `undefined` leak into request URLs.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined

if (!API_BASE_URL) {
  throw new Error('VITE_API_BASE_URL is not set — create frontend/.env.local (see .env.example)')
}

export const env = {
  API_BASE_URL,
} as const
