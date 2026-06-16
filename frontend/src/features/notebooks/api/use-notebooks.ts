import { useQuery } from '@tanstack/react-query'

import { apiClient } from '@/lib/api-client'
import type { NotebookWebResponse } from '@/types/api'

export const notebooksQueryKey = ['notebooks'] as const

// All of the user's notebooks (enabled and disabled) with sync state.
export function useNotebooks() {
  return useQuery({
    queryKey: notebooksQueryKey,
    queryFn: async (): Promise<NotebookWebResponse[]> => {
      const { data } = await apiClient.get<NotebookWebResponse[]>('/api/notebooks')
      return data
    },
    // While any notebook is mid-sync, poll so its badge updates live; stop once
    // none are SYNCING.
    refetchInterval: (query) =>
      query.state.data?.some((notebook) => notebook.sync_status === 'SYNCING') ? 3000 : false,
  })
}
