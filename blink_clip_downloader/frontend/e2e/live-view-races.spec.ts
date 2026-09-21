import { test, expect } from './coverage-fixtures'
import type { Page, Route } from '@playwright/test'

// Starting a live view is slow enough that the user can act again before
// it finishes, and this component has had three separate rounds of
// "the async call outlived the thing that started it" bugs because of it.
// selectCamera() guards against that in two ways, and both need a start
// request genuinely held open to reach:
//
//   * superseded and it *failed*  -> stay quiet, because the error belongs
//     to an attempt nobody is waiting for any more;
//   * superseded and it *worked*  -> stop the session it just created,
//     because this page is the only thing that knows the session exists,
//     and leaking it holds the one live slot until the backend's idle
//     timeout eventually notices.
//
// The second is the expensive one: Blink allows a single live session at a
// time, so a leaked one blocks the feature outright.

/** Hold every liveview start open until the returned release is called. */
function holdStart(page: Page, onStart?: (route: Route) => Promise<boolean>) {
  let unblock: (() => void) | undefined
  const gate = new Promise<void>((resolve) => {
    unblock = resolve
  })
  const seen = { starts: 0, stops: [] as string[] }
  void page.route('**/api/liveview/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/liveview/stop' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { session_id?: string } | null
      seen.stops.push(body?.session_id ?? '')
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"stopped": true}' })
      return
    }
    if (url.pathname === '/api/liveview/start' && route.request().method() === 'POST') {
      seen.starts += 1
      await gate
      if (onStart && (await onStart(route))) return
      await route.fallback()
      return
    }
    await route.fallback()
  })
  return { seen, release: () => unblock?.() }
}

async function openLiveView(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')
}

const STARTING = 'Starting live view…'
const REAL_START_ERROR = /This camera does not support live view/

test('a start that fails after the tab was left does not shout about it', async ({ page }) => {
  const { seen, release } = holdStart(page)
  await openLiveView(page)

  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByText(STARTING)).toBeVisible()
  expect(seen.starts).toBe(1)

  // Leave the tab, which unmounts the page while the start is in flight.
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')

  const landed = page.waitForResponse('**/api/liveview/start')
  release()
  await landed

  // The attempt belongs to a page that no longer exists; surfacing its
  // failure would put an error on an unrelated tab.
  await expect(page.getByText(REAL_START_ERROR)).toHaveCount(0)
  await expect(page.locator('.app-nav-tab.active[data-tab="status"]')).toBeVisible()
})

test('a start that succeeds after the tab was left is torn down, not leaked', async ({ page }) => {
  // The real backend cannot start a session here, so this one is mocked --
  // the point is what the *client* does with a session it asked for and no
  // longer wants, which is the half that leaks a live slot when wrong.
  const { seen, release } = holdStart(page, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ active: true, session_id: 'orphan-session', camera: 'Front Door', state: 'live' }),
    })
    return true
  })
  await openLiveView(page)

  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByText(STARTING)).toBeVisible()

  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')

  const landed = page.waitForResponse('**/api/liveview/start')
  release()
  await landed

  // Nobody is watching it, so it has to be stopped by its own session id.
  await expect.poll(() => seen.stops).toContain('orphan-session')
})

test('picking a second camera mid-start leaves only the newer attempt', async ({ page }) => {
  const { seen, release } = holdStart(page)
  await openLiveView(page)

  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByText(STARTING)).toBeVisible()
  await page.getByRole('button', { name: 'Backyard', exact: true }).click()
  await expect.poll(() => seen.starts).toBe(2)

  release()

  // Both attempts fail against the real backend, but only the one still
  // selected is entitled to say so -- the superseded first attempt must
  // not add a second error for a camera the user already moved off.
  await expect(page.getByText(REAL_START_ERROR).first()).toBeVisible()
  await expect(page.getByText(REAL_START_ERROR)).toHaveCount(1)
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
})
