import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from './auth'
import { useToastStore } from './toast'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

describe('useAuthStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('check(): connected state clears the banner/overlay', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'connected' }))
    const auth = useAuthStore()
    await auth.check()
    expect(auth.state).toBe('connected')
    expect(auth.needsTwoFA).toBe(false)
    expect(auth.bannerVisible).toBe(false)
  })

  it('check(): needs_2fa opens the overlay and resets the form on a fresh transition', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'needs_2fa' }))
    const auth = useAuthStore()
    auth.twoFAMessage = 'stale'
    await auth.check()
    expect(auth.needsTwoFA).toBe(true)
    expect(auth.twoFAMessage).toBe('')
    expect(auth.bannerVisible).toBe(false)
  })

  it('check(): a rejected submission is surfaced and re-enables the form', async () => {
    const auth = useAuthStore()
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ state: 'needs_2fa' }))
    await auth.check()
    auth.pendingSeq = 5
    auth.twoFAPhase = 'submitted'

    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        state: 'needs_2fa',
        two_fa_result_seq: 5,
        two_fa_result_ok: false,
        message: 'Bad code',
      }),
    )
    await auth.check()
    expect(auth.twoFAPhase).toBe('idle')
    expect(auth.twoFAMessageIsError).toBe(true)
    expect(auth.twoFAMessage).toBe('Bad code')
    expect(auth.pendingSeq).toBe(0)
  })

  it('check(): an accepted submission just clears pendingSeq, leaving phase alone', async () => {
    const auth = useAuthStore()
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ state: 'needs_2fa' }))
    await auth.check()
    auth.pendingSeq = 5
    auth.twoFAPhase = 'submitted'

    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ state: 'needs_2fa', two_fa_result_seq: 5, two_fa_result_ok: true }),
    )
    await auth.check()
    expect(auth.twoFAPhase).toBe('submitted')
    expect(auth.twoFAMessageIsError).toBe(false)
    expect(auth.pendingSeq).toBe(0)
  })

  it('check(): error state shows a dismissible banner, not the overlay', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'error', message: 'Bad credentials' }))
    const auth = useAuthStore()
    await auth.check()
    expect(auth.needsTwoFA).toBe(false)
    expect(auth.bannerVisible).toBe(true)
    expect(auth.bannerMessage).toBe('Bad credentials')
  })

  it('check(): a dismissed error message stays suppressed until it changes', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'error', message: 'Bad credentials' }))
    const auth = useAuthStore()
    await auth.check()
    auth.dismissBanner()
    expect(auth.bannerVisible).toBe(false)

    await auth.check()
    expect(auth.bannerVisible).toBe(false)

    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'error', message: 'A new failure' }))
    await auth.check()
    expect(auth.bannerVisible).toBe(true)
    expect(auth.bannerMessage).toBe('A new failure')
  })

  it('check(): transitioning from needs_2fa to connected shows a success toast', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ state: 'needs_2fa' }))
    const auth = useAuthStore()
    await auth.check()

    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ state: 'connected' }))
    await auth.check()
    expect(useToastStore().message).toBe('Signed in to Blink ✓')
  })

  it('check(): a missing state field falls back to disconnected', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    const auth = useAuthStore()
    await auth.check()
    expect(auth.state).toBe('disconnected')
  })

  it('check(): a rejected submission with no message falls back to a default', async () => {
    const auth = useAuthStore()
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ state: 'needs_2fa' }))
    await auth.check()
    auth.pendingSeq = 5
    auth.twoFAPhase = 'submitted'

    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ state: 'needs_2fa', two_fa_result_seq: 5, two_fa_result_ok: false }),
    )
    await auth.check()
    expect(auth.twoFAMessage).toBe('Incorrect verification code. Please try again.')
  })

  it('check(): an error state with no message falls back to a default', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'error' }))
    const auth = useAuthStore()
    await auth.check()
    expect(auth.bannerMessage).toBe('Blink authentication failed.')
  })

  it('check(): a network failure is swallowed silently', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    const auth = useAuthStore()
    await expect(auth.check()).resolves.toBeUndefined()
    expect(auth.state).toBe('disconnected')
  })

  it('submitTwoFA(): rejects non-6-digit input without calling the API', async () => {
    const auth = useAuthStore()
    const ok = await auth.submitTwoFA('123')
    expect(ok).toBe(false)
    expect(fetch).not.toHaveBeenCalled()
    expect(auth.twoFAMessage).toContain('exactly 6 digits')
  })

  it('submitTwoFA(): success sets phase to submitted and stores pendingSeq', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ seq: 42 }))
    const auth = useAuthStore()
    const ok = await auth.submitTwoFA('123 456')
    expect(ok).toBe(true)
    expect(auth.pendingSeq).toBe(42)
    expect(auth.twoFAPhase).toBe('submitted')
    expect(fetch).toHaveBeenCalledWith(
      '/api/auth/2fa',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ code: '123456' }) }),
    )
  })

  it('submitTwoFA(): a non-ok response surfaces the error and resets phase', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse('Invalid code', false))
    const auth = useAuthStore()
    const ok = await auth.submitTwoFA('123456')
    expect(ok).toBe(false)
    expect(auth.twoFAPhase).toBe('idle')
    expect(auth.twoFAMessageIsError).toBe(true)
  })

  it('submitTwoFA(): a network error surfaces a message and resets phase', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    const auth = useAuthStore()
    const ok = await auth.submitTwoFA('123456')
    expect(ok).toBe(false)
    expect(auth.twoFAPhase).toBe('idle')
    expect(auth.twoFAMessage).toContain('Network error')
  })

  it('submitTwoFA(): a response with no seq falls back to 0', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    const auth = useAuthStore()
    await auth.submitTwoFA('123456')
    expect(auth.pendingSeq).toBe(0)
  })

  it('startPolling()/stopPolling() drive check() on an interval', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ state: 'connected' }))
    const auth = useAuthStore()
    auth.startPolling()
    await vi.advanceTimersByTimeAsync(9000)
    expect(fetch).toHaveBeenCalledTimes(4) // immediate + 3 ticks
    auth.stopPolling()
    await vi.advanceTimersByTimeAsync(9000)
    expect(fetch).toHaveBeenCalledTimes(4)
    vi.useRealTimers()
  })
})
