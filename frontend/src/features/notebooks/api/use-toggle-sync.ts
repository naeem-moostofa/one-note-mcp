import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { notebooksQueryKey } from '@/features/notebooks/api/use-notebooks'
import { apiClient } from '@/lib/api-client'
import type { NotebookWebResponse } from '@/types/api'

interface ToggleArgs {
  id: number
  syncEnabled: boolean
}

// PATCH /api/notebooks/{id} — flips sync_enabled and returns the authoritative row.
// We refetch list pages after success instead of optimistically editing filtered
// pages; that keeps membership, totals, pagination, and ordering backend-owned.
export function useToggleSync() {
  const queryClient = useQueryClient()
  const [pendingNotebookIds, setPendingNotebookIds] = useState<ReadonlySet<number>>(() => new Set())

  const mutation = useMutation({
    mutationFn: async ({ id, syncEnabled }: ToggleArgs): Promise<NotebookWebResponse> => {
      const { data } = await apiClient.patch<NotebookWebResponse>(`/api/notebooks/${id}`, {
        sync_enabled: syncEnabled,
      })
      return data
    },
    onMutate: ({ id }) => {
      setPendingNotebookIds((current) => new Set(current).add(id))
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: notebooksQueryKey })
    },
    onSettled: (_data, _error, variables) => {
      if (!variables) {
        return
      }
      setPendingNotebookIds((current) => {
        const next = new Set(current)
        next.delete(variables.id)
        return next
      })
    },
  })

  return { ...mutation, pendingNotebookIds }
}
