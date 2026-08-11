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

export const test = base.extend({
  page: async ({ page }, use) => {
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
