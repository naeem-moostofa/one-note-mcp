import { useQuery } from '@tanstack/react-query'

import { apiClient } from '@/lib/api-client'
import type { NotebookFilter, NotebookWebResponse, PaginatedResponse } from '@/types/api'

export const notebooksQueryKey = ['notebooks'] as const
export const notebooksQueryKeyFor = (filters: NotebookFilter) => [...notebooksQueryKey, filters] as const

// One filtered, paginated page of the user's notebooks (enabled and disabled) with
// sync state. The backend promotes syncing/synced notebooks first, then orders each
// bucket by "last edited" (newest first), so no client-side sort is needed.
export function useNotebooks(filters: NotebookFilter = {}) {
  return useQuery({
    queryKey: notebooksQueryKeyFor(filters),
    queryFn: async (): Promise<PaginatedResponse<NotebookWebResponse>> => {
      const { data } = await apiClient.get<PaginatedResponse<NotebookWebResponse>>('/api/notebooks', { params: filters })
      return data
    },
    // While any notebook on the current page is mid-sync, poll so its badge updates
    // live; stop once none are SYNCING.
    refetchInterval: (query) =>
      query.state.data?.data.some((notebook) => notebook.sync_status === 'SYNCING') ? 3000 : false,
  })
}
