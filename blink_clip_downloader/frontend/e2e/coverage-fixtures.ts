import { test as base, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

// Every e2e spec imports `test`/`expect` from here instead of directly
// from '@playwright/test', so every test automatically gets its coverage
// collected with no per-file boilerplate — see playwright.config.ts's
// comment on how the build this runs against gets instrumented in the
// first place (vite.config.ts's istanbul plugin, VITE_COVERAGE=true only).
const COVERAGE_DIR = path.join(process.cwd(), '.nyc_output')
const HA_INGRESS_URL = process.env.BLINK_E2E_HA === '1' ? process.env.HA_E2E_INGRESS_URL : undefined

export function appPathname(requestUrl: string): string {
  return new URL(requestUrl).pathname.replace(/^\/api\/hassio_ingress\/[^/]+/, '') || '/'
}

export function appApiUrl(pathname: string): string {
  if (!HA_INGRESS_URL) return pathname
  const ingressBase = new URL(HA_INGRESS_URL)
  return new URL(pathname.replace(/^\/+/, ''), ingressBase).toString()
}

export function ingressNavigationUrl(requestUrl: string, ingressBase: URL): string {
  const url = new URL(requestUrl, ingressBase)
  if (url.origin !== ingressBase.origin) return requestUrl

  const ingressPath = ingressBase.pathname.replace(/\/+$/, '')
  if (url.pathname === ingressPath || url.pathname.startsWith(`${ingressPath}/`)) {
    return url.toString()
  }

  const appPath = url.pathname.replace(/^\/+/, '')
  return new URL(`${ingressPath}/${appPath}${url.search}${url.hash}`, ingressBase.origin).toString()
}

export const test = base.extend({
  page: async ({ page }, use) => {
    if (HA_INGRESS_URL) {
      const ingressBase = new URL(HA_INGRESS_URL)
      const originalGoto = page.goto.bind(page)
      // A leading slash otherwise makes Playwright discard the ingress prefix in baseURL.
      page.goto = (url, options) => originalGoto(ingressNavigationUrl(url, ingressBase), options)

      await page.route('**/*', async (route) => {
        const request = route.request()
        const url = new URL(request.url())
        if (!request.isNavigationRequest() || url.origin !== ingressBase.origin) {
          await route.fallback()
          return
        }

        await route.continue({ url: ingressNavigationUrl(request.url(), ingressBase) })
      })
    }

    await use(page)
    if (process.env.VITE_COVERAGE !== 'true') return
    // window.__coverage__ only exists when the served build was actually
    // instrumented — absent (undefined) on a plain `npm run test:e2e` run,
    // which is the common case and not an error.
    const coverage = await page.evaluate(() => (window as unknown as { __coverage__?: unknown }).__coverage__)
    if (!coverage) return
    mkdirSync(COVERAGE_DIR, { recursive: true })
    writeFileSync(path.join(COVERAGE_DIR, `coverage-${randomUUID()}.json`), JSON.stringify(coverage))
  },
})

export { expect }
