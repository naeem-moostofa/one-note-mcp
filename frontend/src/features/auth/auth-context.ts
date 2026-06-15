import { createContext } from 'react'

export interface AuthContextValue {
  token: string | null
  isAuthenticated: boolean
  signOut: () => void
}

// Context identity lives in its own (non-component) module so the provider file
// can export only the component — keeps React Fast Refresh happy and separates
// the context's identity from its implementation.
export const AuthContext = createContext<AuthContextValue | undefined>(undefined)
