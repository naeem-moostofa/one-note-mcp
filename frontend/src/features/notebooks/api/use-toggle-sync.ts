import { useMutation, useQueryClient } from '@tanstack/react-query'

import { notebooksQueryKey } from '@/features/notebooks/api/use-notebooks'
import { apiClient } from '@/lib/api-client'
import type { NotebookWebResponse, PaginatedResponse } from '@/types/api'

type NotebookPage = PaginatedResponse<NotebookWebResponse>

interface ToggleArgs {
  id: number
  syncEnabled: boolean
}

// PATCH /api/notebooks/{id} — flips sync_enabled. Optimistic: the switch moves
// instantly and rolls back if the request fails (deterministic single-field flip).
export function useToggleSync() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, syncEnabled }: ToggleArgs): Promise<void> => {
      await apiClient.patch(`/api/notebooks/${id}`, { sync_enabled: syncEnabled }) // 204
    },
    onMutate: async ({ id, syncEnabled }: ToggleArgs) => {
      await queryClient.cancelQueries({ queryKey: notebooksQueryKey })
      const previous = queryClient.getQueriesData<NotebookPage>({ queryKey: notebooksQueryKey })
      // Update only page.data — total/limit/offset are unchanged by a field flip.
      queryClient.setQueriesData<NotebookPage>({ queryKey: notebooksQueryKey }, (current) =>
        current && {
          ...current,
          data: current.data.map((notebook) =>
            notebook.id === id ? { ...notebook, sync_enabled: syncEnabled } : notebook,
          ),
        },
      )
      return { previous }
    },
    onError: (_error, _args, context) => {
      if (context?.previous) {
        for (const [queryKey, page] of context.previous) {
          queryClient.setQueryData(queryKey, page)
        }
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: notebooksQueryKey })
    },
  })
}
