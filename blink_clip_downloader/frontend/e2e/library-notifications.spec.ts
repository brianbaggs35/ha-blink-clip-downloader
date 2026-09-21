import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// LibraryPage raises a browser notification when a /api/stats poll reports
// more clips than the previous poll did (checkNewClipsNotification). Every
// condition on that path needs a real browser to reach: the opt-in flag in
// localStorage, a real Notification permission state, an already-established
// previous count, and two polls in the same page session. The component's
// unit spec stubs all of that in jsdom, which proves the function's logic
// but not that the app ever actually gets there -- the poll that calls it
// is wired up three layers away, in loadStats() via loadAll() via the
// cross-tab refresh signal.
//
// Nothing here touches the backend's own data: /api/stats is fetched for
// real and only its total_count is rewritten on the way back, so the rest
// of the page (and the shared database) is untouched.

/** Install a recording stand-in for window.Notification before app code runs. */
async function stubNotifications(page: Page, options: { enabled: boolean; permission: string }) {
  await page.addInitScript(
    ({ enabled, permission }) => {
      if (enabled) localStorage.setItem('blink_notif', '1')
      else localStorage.removeItem('blink_notif')

      const fired: { title: string; body?: string; tag?: string }[] = []
      class RecordingNotification {
        static permission = permission
        static requestPermission = async () => permission
        constructor(title: string, opts?: { body?: string; tag?: string }) {
          fired.push({ title, body: opts?.body, tag: opts?.tag })
        }
      }
      // defineProperty rather than assignment: Notification is a read-only
      // accessor on window in Chromium, so a plain `window.Notification = `
      // is silently dropped and the real (permission: "default") one stays.
      Object.defineProperty(window, 'Notification', {
        value: RecordingNotification,
        configurable: true,
        writable: true,
      })
      Object.defineProperty(window, '__firedNotifications', { value: fired, configurable: true })
    },
    { enabled: options.enabled, permission: options.permission },
  )
}

/**
 * Rewrite only total_count on the real /api/stats response, and count how
 * many times we have served it. The count is what makes the "stays silent"
 * tests meaningful: asserting "no notification fired" is worthless unless
 * the poll that would have fired one has demonstrably already happened.
 */
async function stubClipTotal(page: Page, total: () => number) {
  const state = { served: 0 }
  await page.route('**/api/stats', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback()
      return
    }
    const response = await route.fetch()
    const body = (await response.json()) as Record<string, unknown>
    state.served += 1
    await route.fulfill({ response, json: { ...body, total_count: total() } })
  })
  return state
}

const fired = (page: Page) =>
  page.evaluate(() => (window as unknown as { __firedNotifications: { title: string }[] }).__firedNotifications)

async function openLibrary(page: Page) {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  // The first poll only records a baseline — checkNewClipsNotification
  // deliberately stays quiet until it has a previous count to compare to,
  // so that opening the app never announces the whole existing library.
  await expect.poll(async () => (await fired(page)).length).toBe(0)
}

/** Click Refresh and wait until the resulting /api/stats poll has landed. */
async function refreshAndSettle(page: Page, served: { served: number }) {
  const before = served.served
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => served.served).toBeGreaterThan(before)
}

test('announces new clips, and gets the singular and plural wording right', async ({ page }) => {
  let total = 10
  await stubNotifications(page, { enabled: true, permission: 'granted' })
  const served = await stubClipTotal(page, () => total)
  await openLibrary(page)

  total = 11
  await refreshAndSettle(page, served)
  await expect.poll(async () => (await fired(page)).map((n) => n.title)).toEqual(['🎥 1 new Blink clip'])

  // Two more at once: the count in the message is the difference since the
  // last poll, not the total, and it has to pluralize.
  total = 13
  await refreshAndSettle(page, served)
  await expect
    .poll(async () => (await fired(page)).map((n) => n.title))
    .toEqual(['🎥 1 new Blink clip', '🎥 2 new Blink clips'])

  const [first] = await fired(page)
  expect(first.body).toBe('New clips are available in your library.')
  // A fixed tag so a burst of polls replaces one notification rather than
  // stacking a pile of them in the OS tray.
  expect(first.tag).toBe('blink-new-clips')
})

test('stays silent when the clip count has not grown', async ({ page }) => {
  let total = 10
  await stubNotifications(page, { enabled: true, permission: 'granted' })
  const served = await stubClipTotal(page, () => total)
  await openLibrary(page)

  // A clip deleted between polls must not be announced as an arrival.
  total = 8
  await refreshAndSettle(page, served)
  expect(await fired(page)).toEqual([])
})

test('stays silent when notifications are switched off', async ({ page }) => {
  let total = 10
  await stubNotifications(page, { enabled: false, permission: 'granted' })
  const served = await stubClipTotal(page, () => total)
  await openLibrary(page)

  total = 12
  await refreshAndSettle(page, served)
  await refreshAndSettle(page, served)
  expect(await fired(page)).toEqual([])
})

test('stays silent when permission was never granted', async ({ page }) => {
  let total = 10
  await stubNotifications(page, { enabled: true, permission: 'default' })
  const served = await stubClipTotal(page, () => total)
  await openLibrary(page)

  total = 12
  await refreshAndSettle(page, served)
  await refreshAndSettle(page, served)
  expect(await fired(page)).toEqual([])
})
