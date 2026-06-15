import { QueryClient } from '@tanstack/react-query'

// Shared QueryClient. Defaults tuned for V1: data is fresh for 30s (notebooks /
// connections don't change second-to-second), and 4xx responses are never retried
// (a 401/403/404 won't fix itself on retry — only transient/network errors get one).
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: (failureCount, error) => {
        const status = (error as { response?: { status?: number } })?.response?.status
        if (status !== undefined && status >= 400 && status < 500) {
          return false
        }
        return failureCount < 1
      },
    },
  },
})
