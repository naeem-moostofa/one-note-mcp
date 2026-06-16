import { useMutation, useQueryClient } from '@tanstack/react-query'

import { notebooksQueryKey } from '@/features/notebooks/api/use-notebooks'
import { apiClient } from '@/lib/api-client'

// POST /api/notebooks/refresh — names-only discovery from Microsoft Graph, so the
// list can be populated right after connecting (before any full sync). Returns the
// same shape as GET /api/notebooks. Raises 409 if there's no active Microsoft connection.
export function useRefreshNotebooks() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<void> => {
      await apiClient.post('/api/notebooks/refresh')
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: notebooksQueryKey })
    },
  })
}
