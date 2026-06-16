import { useMutation, useQueryClient } from '@tanstack/react-query'

import { notebooksQueryKey } from '@/features/notebooks/api/use-notebooks'
import { apiClient } from '@/lib/api-client'
import type { NotebookWebResponse } from '@/types/api'

// POST /api/notebooks/{id}/sync — kicks off a background notebook sync (sections,
// pages, OCR) for one notebook. The endpoint returns immediately with the list
// (notebook now SYNCING); polling in use-notebooks watches it reach FRESH/FAILED.
export function useSyncNotebook() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (notebookId: number): Promise<NotebookWebResponse[]> => {
      const { data } = await apiClient.post<NotebookWebResponse[]>(`/api/notebooks/${notebookId}/sync`)
      return data
    },
    onSuccess: (notebooks) => {
      // Seed the cache so the SYNCING badge appears immediately; polling takes over.
      queryClient.setQueryData(notebooksQueryKey, notebooks)
    },
  })
}
