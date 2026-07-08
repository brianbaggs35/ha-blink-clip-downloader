// Hand-written types mirroring the aiohttp backend's JSON payloads (see
// media_server.py's route handlers). No OpenAPI/pydantic schema exists to
// generate these from — aiohttp has no schema layer in this project — so
// this file is the single source of truth on the frontend side and must be
// kept in sync by hand as each tab is ported.

export type TwoFAState = 'connected' | 'needs_2fa' | 'error' | 'disconnected'

export interface AuthStatus {
  state: TwoFAState
  message?: string
  two_fa_result_seq?: number
  two_fa_result_ok?: boolean
}
