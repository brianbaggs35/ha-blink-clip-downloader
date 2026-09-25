import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAccessStore } from './access'
import { goTo } from '../navigation'

vi.mock('../navigation', () => ({ goTo: vi.fn() }))

function jsonResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('useAccessStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(goTo).mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('starts as sign-in off with no token', () => {
    const access = useAccessStore()
    expect(access.loginEnabled).toBe(false)
    expect(access.via).toBe('open')
    expect(access.accessToken).toBe('')
    expect(access.signedIn).toBe(false)
  })

  it('load() takes the server state', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ login_enabled: true, via: 'session', user: 'brian', access_token: 'tok' }),
    )
    const access = useAccessStore()
    await access.load()
    expect(access.loginEnabled).toBe(true)
    expect(access.user).toBe('brian')
    expect(access.accessToken).toBe('tok')
    expect(access.signedIn).toBe(true)
  })

  it('load() keeps the defaults when the request fails', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    const access = useAccessStore()
    await access.load()
    expect(access.via).toBe('open')
    expect(access.accessToken).toBe('')
  })

  it('regenerateToken() stores and returns the new token', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ access_token: 'new' }))
    const access = useAccessStore()
    await expect(access.regenerateToken()).resolves.toBe('new')
    expect(access.accessToken).toBe('new')
  })

  it('signOut() goes to the login page', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ signed_out: true }))
    await useAccessStore().signOut()
    expect(fetch).toHaveBeenCalledWith('/logout', expect.objectContaining({ method: 'POST' }))
    expect(goTo).toHaveBeenCalledWith('/login')
  })

  it('signOut() goes to the login page even when the request fails', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    await useAccessStore().signOut()
    expect(goTo).toHaveBeenCalledWith('/login')
  })
})
