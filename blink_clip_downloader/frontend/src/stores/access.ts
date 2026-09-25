import { defineStore } from 'pinia'
import { getAccess, regenerateAccessToken, signOut } from '../api/access'
import type { AccessStatus } from '../api/types'
import { goTo } from '../navigation'

/** Direct-port sign-in state, loaded once by AppSidebar. Shared because two
 *  unrelated places read it: the sidebar's Sign out button (shown only for
 *  a browser signed in on the direct port) and the Automations tab, which
 *  puts `accessToken` into the YAML it generates for Home Assistant's own
 *  calls to the add-on. Until it loads it reads as sign-in off with no
 *  token, which generates exactly the YAML the add-on produced before 6.0.8. */
export const useAccessStore = defineStore('access', {
  state: () => ({
    loginEnabled: false,
    via: 'open' as AccessStatus['via'],
    user: null as string | null,
    accessToken: '',
  }),
  getters: {
    /** Signed in on the direct port, so there is something to sign out of. */
    signedIn: (state) => state.via === 'session',
  },
  actions: {
    async load() {
      try {
        const status = await getAccess()
        this.loginEnabled = status.login_enabled
        this.via = status.via
        this.user = status.user
        this.accessToken = status.access_token
      } catch {
        // Leave the defaults: no Sign out button, and YAML without a token.
      }
    },
    async regenerateToken(): Promise<string> {
      const { access_token } = await regenerateAccessToken()
      this.accessToken = access_token
      return access_token
    },
    async signOut() {
      try {
        await signOut()
      } catch {
        // The login page is still where to go: if the cookie survived, it
        // sends a signed-in browser straight back.
      }
      goTo('/login')
    },
  },
})
