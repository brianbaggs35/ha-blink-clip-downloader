import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// CameraConfigsSection has to defend a form someone is typing in against
// the app's own background refreshes. The cross-tab refresh signal fires
// for reasons that have nothing to do with camera configs -- a clip
// starred, AI feedback submitted from a clip panel, the sidebar's Refresh
// button -- and a naive reload would replace a half-written camera
// description with whatever the server last stored, with no warning and
// nothing to undo it.
//
// Two guards do that, and neither is reachable without a real browser:
//   1. the refresh watcher declines to *start* a load over a dirty form;
//   2. load() drops a result that arrived after the form went dirty,
//      which needs a response genuinely in flight at the moment of typing.
//
// Both are observed here through their real trigger rather than by calling
// the component -- and the AI page's own /api/ai/status refetch on the same
// signal is what makes "the refresh definitely happened" checkable instead
// of a sleep.

const CAMERA = 'Front Door'
const descBox = (page: Page) => page.locator(`[id="cam-desc-${CAMERA}"]`)

/** Count GETs per endpoint so a test can prove a reload did or did not run. */
async function countGets(page: Page) {
  const counts = { configs: 0, status: 0 }
  page.on('request', (request) => {
    if (request.method() !== 'GET') return
    const path = new URL(request.url()).pathname
    if (path === '/api/ai/camera-configs') counts.configs += 1
    if (path === '/api/ai/status') counts.status += 1
  })
  return counts
}

/**
 * Hold every camera-configs GET open until the returned function is called.
 * Returns a releaser rather than a plain promise so a test reads in the
 * order things happen: hold, act, release.
 */
function holdAll(page: Page) {
  let unblock: (() => void) | undefined
  const gate = new Promise<void>((resolve) => {
    unblock = resolve
  })
  void page.route('**/api/ai/camera-configs', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback()
      return
    }
    await gate
    await route.fallback()
  })
  return async () => {
    unblock?.()
    await page.unroute('**/api/ai/camera-configs')
  }
}

async function openAiTabAndExpand(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()
  await expect(descBox(page)).toBeVisible()
}

/** Click the sidebar Refresh and wait until the signal has demonstrably landed. */
async function refreshAndWaitForTick(page: Page, counts: { status: number }) {
  const before = counts.status
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => counts.status).toBeGreaterThan(before)
}

test.afterEach(async ({ page }) => {
  // Nothing here ever saves, so the stored config is untouched -- but a
  // dirty form left behind would be discarded by the reload anyway. Reload
  // to a clean page so the next spec starts from the stored state.
  await page.goto('/')
})

test('a background refresh leaves a half-typed camera description alone', async ({ page }) => {
  const counts = await countGets(page)
  await openAiTabAndExpand(page)
  await expect.poll(() => counts.configs).toBeGreaterThan(0)

  const typed = 'Half-typed description that a refresh must not eat'
  await descBox(page).fill(typed)
  const configFetchesBefore = counts.configs

  await refreshAndWaitForTick(page, counts)
  // AiAnalysisConfigCard reloads camera-configs on the same signal, and it
  // is *supposed* to -- so the request count cannot tell the two consumers
  // apart. What it does give us is a hard deadline: once a camera-configs
  // response has landed in this page, any reload this section had started
  // would have landed with it. The box still holding what was typed is
  // therefore the guard working, not a race we got lucky on.
  await expect.poll(() => counts.configs).toBeGreaterThan(configFetchesBefore)

  await expect(descBox(page)).toHaveValue(typed)
})

test('a reload already in flight is discarded if typing starts before it lands', async ({ page }) => {
  const counts = await countGets(page)
  await openAiTabAndExpand(page)
  await expect.poll(() => counts.configs).toBeGreaterThan(0)

  // Hold *every* configs GET open long enough to type into the form while
  // one is still in flight -- the exact race the second guard exists for.
  // Holding only the first is not enough: AiAnalysisConfigCard fetches the
  // same endpoint on the same signal, so the first request in flight is
  // just as likely to be its, leaving this section's own reload to land
  // before any typing happens and the race never to occur.
  const release = holdAll(page)

  // The form is clean, so the watcher lets this load start.
  const configFetchesBefore = counts.configs
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => counts.configs).toBeGreaterThan(configFetchesBefore)

  const typed = 'Typed while the reload was still in flight'
  await descBox(page).fill(typed)
  await release()

  // The response lands now, carrying the stored (empty) description. It
  // must be thrown away rather than applied over what was just typed.
  await expect(descBox(page)).toHaveValue(typed)
  await expect(descBox(page)).toHaveValue(typed, { timeout: 2000 })
})

test('a background refresh never blanks the section to a spinner', async ({ page }) => {
  const counts = await countGets(page)
  await openAiTabAndExpand(page)

  const release = holdAll(page)

  const configFetchesBefore = counts.configs
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => counts.configs).toBeGreaterThan(configFetchesBefore)

  // Mid-flight: the already-rendered form stays on screen. Only the very
  // first load, with nothing to show yet, is allowed to show a spinner.
  const section = page.locator('.card', { hasText: '📷 Camera Configurations' })
  await expect(section.locator('.loading-indicator')).toHaveCount(0)
  await expect(descBox(page)).toBeVisible()

  await release()
  await expect(descBox(page)).toBeVisible()
})
