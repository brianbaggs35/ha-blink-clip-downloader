import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getAccess, regenerateAccessToken, signOut } from './access'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('access api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getAccess()', async () => {
    await getAccess()
    expect(fetch).toHaveBeenCalledWith('/api/access', {})
  })

  it('regenerateAccessToken()', async () => {
    await regenerateAccessToken()
    expect(fetch).toHaveBeenCalledWith('/api/access/token/regenerate', { method: 'POST' })
  })

  it('signOut()', async () => {
    await signOut()
    expect(fetch).toHaveBeenCalledWith('/logout', { method: 'POST' })
  })
})
