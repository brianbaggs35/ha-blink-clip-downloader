import { apiGet, apiPost } from './client'
import type { AccessStatus } from './types'

export function getAccess(): Promise<AccessStatus> {
  return apiGet('/api/access')
}

export function regenerateAccessToken(): Promise<{ access_token: string }> {
  return apiPost('/api/access/token/regenerate')
}

export function signOut(): Promise<{ signed_out: boolean }> {
  return apiPost('/logout')
}
