import { useMutation, useQueryClient } from '@tanstack/react-query'

import { notebooksQueryKey } from '@/features/notebooks/api/use-notebooks'
import { apiClient } from '@/lib/api-client'
import type { NotebookWebResponse, PaginatedResponse } from '@/types/api'

type NotebookPage = PaginatedResponse<NotebookWebResponse>

// POST /api/notebooks/{id}/sync — kicks off a background notebook sync (sections,
// pages, OCR) for one notebook. The endpoint returns 202 with no body; we optimistically
// flip the row to SYNCING so the badge shows immediately, then invalidate so polling
// in use-notebooks watches it reach FRESH/FAILED.
export function useSyncNotebook() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (notebookId: number): Promise<void> => {
      await apiClient.post(`/api/notebooks/${notebookId}/sync`) // 202, no body
    },
    onMutate: async (notebookId: number) => {
      await queryClient.cancelQueries({ queryKey: notebooksQueryKey })
      const previous = queryClient.getQueriesData<NotebookPage>({ queryKey: notebooksQueryKey })
      // Update only page.data in place so search results keep their order.
      queryClient.setQueriesData<NotebookPage>({ queryKey: notebooksQueryKey }, (current) =>
        current && {
          ...current,
          data: current.data.map((notebook) =>
            notebook.id === notebookId ? { ...notebook, sync_status: 'SYNCING' } : notebook,
          ),
        },
      )
      return { previous }
    },
    onError: (_error, _notebookId, context) => {
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
