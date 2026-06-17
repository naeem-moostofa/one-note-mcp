import { useMutation } from '@tanstack/react-query'

import { apiClient } from '@/lib/api-client'
import type { CreateMCPConnectionRequest, MCPConnectionCreated } from '@/types/api'

export function useCreateMCPConnection() {
  return useMutation({
    mutationFn: async (body: CreateMCPConnectionRequest): Promise<MCPConnectionCreated> => {
      const { data } = await apiClient.post<MCPConnectionCreated>('/api/mcp-connections', body)
      return data
    },
  })
}
