import { useQuery } from '@tanstack/react-query'

import { apiClient } from '@/lib/api-client'
import type { MeResponse } from '@/types/api'

export const meQueryKey = ['me'] as const

// The signed-in user's profile + Microsoft connection status. This is the single
// source of truth for "who am I" — the auth context only holds the credential.
export function useMe() {
  return useQuery({
    queryKey: meQueryKey,
    queryFn: async (): Promise<MeResponse> => {
      const { data } = await apiClient.get<MeResponse>('/api/me')
      return data
    },
  })
}
