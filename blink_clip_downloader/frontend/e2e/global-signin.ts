import { request, type FullConfig } from '@playwright/test'
import { E2E_LOGIN, SIGNED_IN_STATE } from './signin-state'

/** With BLINK_E2E_SIGNIN=1 the backend asks for a sign-in on its main port,
 *  as the add-on's direct port does for a browser that is not Home
 *  Assistant's ingress. Sign in once here, through the real login form
 *  endpoint, so every spec then starts with the session cookie a signed-in
 *  browser carries and the suite exercises the app as that browser sees it. */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL
  if (!baseURL) throw new Error('playwright.config.ts sets no baseURL to sign in on')
  const api = await request.newContext({ baseURL })
  try {
    const response = await api.post('/login', { form: E2E_LOGIN, maxRedirects: 0 })
    if (response.status() !== 303) {
      throw new Error(`Signing in on ${baseURL} returned HTTP ${response.status()}, not 303`)
    }
    await api.storageState({ path: SIGNED_IN_STATE })
  } finally {
    await api.dispose()
  }
}
