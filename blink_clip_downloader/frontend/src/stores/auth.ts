import { defineStore } from 'pinia'
import { INGRESS_ROOT } from '../env'
import type { AuthStatus, TwoFAState } from '../api/types'
import { useToastStore } from './toast'

const POLL_INTERVAL_MS = 3000

export const useAuthStore = defineStore('auth', {
  state: () => ({
    state: 'disconnected' as TwoFAState,
    // A plain auth failure (bad/placeholder credentials, no 2FA involved) is
    // common during setup/testing and must never trap the user behind a
    // full-screen modal — only an actual pending 2FA code (which truly
    // blocks everything else) gets the modal treatment. See bannerMessage
    // vs. needsTwoFA below.
    bannerMessage: '',
    bannerVisible: false,
    twoFAMessage: '',
    twoFAMessageIsError: false,
    // 'idle': form usable. 'verifying': POST in flight. 'submitted': POST
    // accepted, waiting on the next poll to confirm/reject — button stays
    // disabled through both 'verifying' and 'submitted'.
    twoFAPhase: 'idle' as 'idle' | 'verifying' | 'submitted',
    // Suppresses the banner once the user dismisses it, until the message
    // actually changes (e.g. a fresh failure) — otherwise every 3s poll
    // while credentials remain bad would keep popping it back open.
    dismissedMessage: null as string | null,
    pendingSeq: 0,
    pollHandle: undefined as ReturnType<typeof setInterval> | undefined,
  }),
  getters: {
    needsTwoFA: (state) => state.state === 'needs_2fa',
    twoFASubmitting: (state) => state.twoFAPhase !== 'idle',
  },
  actions: {
    startPolling() {
      void this.check()
      this.pollHandle = setInterval(() => void this.check(), POLL_INTERVAL_MS)
    },
    stopPolling() {
      clearInterval(this.pollHandle)
    },
    // Handles the post-fetch state sync for the `needs_2fa` branch of
    // check() — split out purely to keep check() under the cognitive
    // complexity limit; behavior is unchanged.
    _syncNeedsTwoFA(prev: TwoFAState, status: AuthStatus) {
      this.bannerVisible = false
      if (prev !== 'needs_2fa') {
        this.twoFAMessage = ''
        this.pendingSeq = 0
      }
      // If the submission we were waiting on came back rejected, let the
      // user try again instead of leaving the form stuck on "Verifying…".
      if (this.pendingSeq && status.two_fa_result_seq === this.pendingSeq) {
        if (status.two_fa_result_ok === false) {
          this.twoFAMessage = status.message || 'Incorrect verification code. Please try again.'
          this.twoFAMessageIsError = true
          this.twoFAPhase = 'idle'
        }
        this.pendingSeq = 0
      }
    },
    _syncError(status: AuthStatus) {
      this.pendingSeq = 0
      const msg = status.message || 'Blink authentication failed.'
      if (msg !== this.dismissedMessage) {
        this.bannerMessage = msg
        this.bannerVisible = true
      }
    },
    _syncConnectedOrDisconnected(prev: TwoFAState) {
      if ((prev === 'needs_2fa' || prev === 'error') && this.state === 'connected') {
        useToastStore().show('Signed in to Blink ✓')
      }
      this.bannerVisible = false
      this.dismissedMessage = null
      this.pendingSeq = 0
    },
    async check() {
      let status: AuthStatus
      try {
        const res = await fetch(INGRESS_ROOT + '/api/auth/status')
        status = await res.json()
      } catch {
        return
      }
      const prev = this.state
      this.state = status.state || 'disconnected'

      if (this.state === 'needs_2fa') {
        this._syncNeedsTwoFA(prev, status)
      } else if (this.state === 'error') {
        this._syncError(status)
      } else {
        this._syncConnectedOrDisconnected(prev)
      }
    },
    dismissBanner() {
      this.dismissedMessage = this.bannerMessage
      this.bannerVisible = false
    },
    async submitTwoFA(rawCode: string): Promise<boolean> {
      const code = rawCode.trim().replace(/\s/g, '')
      if (!/^\d{6}$/.test(code)) {
        this.twoFAMessage = 'Please enter exactly 6 digits.'
        this.twoFAMessageIsError = false
        return false
      }
      this.twoFAPhase = 'verifying'
      this.twoFAMessage = 'Submitting code…'
      this.twoFAMessageIsError = false
      try {
        const resp = await fetch(INGRESS_ROOT + '/api/auth/2fa', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code }),
        })
        if (!resp.ok) {
          this.twoFAMessage = await resp.text().catch(() => 'Error')
          this.twoFAMessageIsError = true
          this.twoFAPhase = 'idle'
          return false
        }
        const data = await resp.json().catch(() => ({}))
        this.pendingSeq = data.seq || 0
        // Keep the button disabled — check() polls every 3s and will clear
        // the overlay on success, or reset this to 'idle' with an error
        // message if Blink rejects the code.
        this.twoFAPhase = 'submitted'
        this.twoFAMessage = 'Code submitted — waiting for confirmation…'
        this.twoFAMessageIsError = false
        return true
      } catch {
        this.twoFAMessage = 'Network error — could not submit code.'
        this.twoFAMessageIsError = true
        this.twoFAPhase = 'idle'
        return false
      }
    },
  },
})
