import { test, expect } from './coverage-fixtures'
import type { Page, Route } from '@playwright/test'

// Saving camera configs is a read-modify-write of one shared JSON file, so
// the client sends the revision it read (If-Match) and the server rejects
// the write with 409 if anything changed underneath it. updateCameraConfigs
// then re-reads and rebuilds once before giving up -- which is what stops
// two tabs, or the AI and Vehicles tabs of one tab's own UI, from having to
// be saved in a particular order.
//
// None of that is visible from a single-tab happy path, so it is driven
// here by failing the first PUT the way a real conflict would.

const CAMERA = 'Front Door'
const descBox = (page: Page) => page.locator(`[id="cam-desc-${CAMERA}"]`)

async function openAiTabAndExpand(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()
  await expect(descBox(page)).toBeVisible()
}

/** Reject the first *n* PUTs with 409, then let the rest through. */
async function conflictFirst(page: Page, n: number) {
  const seen = { puts: 0, gets: 0 }
  await page.route('**/api/ai/camera-configs', async (route: Route) => {
    const method = route.request().method()
    if (method === 'GET') {
      seen.gets += 1
      await route.fallback()
      return
    }
    if (method !== 'PUT') {
      await route.fallback()
      return
    }
    seen.puts += 1
    if (seen.puts <= n) {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'camera configs changed underneath this save' }),
      })
      return
    }
    await route.fallback()
  })
  return seen
}

/** Read the stored configs over HTTP rather than through the UI. */
async function storedDescription(page: Page, camera: string): Promise<string> {
  const response = await page.request.get('/api/ai/camera-configs')
  const configs = (await response.json()) as { camera: string; description?: string }[]
  return configs.find((config) => config.camera === camera)?.description ?? ''
}

test.afterEach(async ({ page }) => {
  // Put the stored description back over HTTP, not by driving the UI. A
  // navigation here would wipe window.__coverage__ before
  // coverage-fixtures.ts reads it, throwing away everything the test just
  // exercised -- and this cleanup does not need a browser page at all.
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  const response = await page.request.get('/api/ai/camera-configs')
  const configs = (await response.json()) as Record<string, unknown>[]
  await page.request.put('/api/ai/camera-configs', {
    data: configs.map((config) => (config.camera === CAMERA ? { ...config, description: '' } : config)),
  })
  expect(await storedDescription(page, CAMERA)).toBe('')
})

test('a save that loses a race is re-read and retried once, not lost', async ({ page }) => {
  const seen = await conflictFirst(page, 1)
  await openAiTabAndExpand(page)

  const typed = 'Written by a save that had to retry'
  await descBox(page).fill(typed)
  const getsBeforeSave = seen.gets
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()

  await expect(page.getByText('Camera configs saved')).toBeVisible()
  // Two PUTs: the rejected one and the retry. The retry re-read first, so
  // it rebuilt against whatever the other writer had left behind rather
  // than replaying a stale array over the top of it.
  expect(seen.puts).toBe(2)
  expect(seen.gets).toBeGreaterThan(getsBeforeSave + 1)

  // Checked over HTTP rather than with a reload, for the same reason the
  // cleanup above avoids one.
  expect(await storedDescription(page, CAMERA)).toBe(typed)
})

test('a conflict that repeats is reported rather than retried forever', async ({ page }) => {
  const seen = await conflictFirst(page, 99)
  await openAiTabAndExpand(page)

  await descBox(page).fill('A save that never wins the race')
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()

  await expect(page.getByText('Failed to save camera configs')).toBeVisible()
  // Exactly one retry, then it gives up — an unbounded loop here would
  // hammer the add-on for as long as the other writer kept winning.
  expect(seen.puts).toBe(2)
})

test('a camera-alias header that is not JSON does not stop a save', async ({ page }) => {
  // The aliases ride along on a response header, so a truncated or
  // malformed one has to degrade to "no aliases" rather than throw and
  // take the whole save with it.
  await page.route('**/api/ai/camera-configs', async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback()
      return
    }
    const response = await route.fetch()
    await route.fulfill({
      response,
      headers: { ...response.headers(), 'x-camera-aliases': '{not json at all' },
    })
  })
  await openAiTabAndExpand(page)

  const typed = 'Saved despite a broken alias header'
  await descBox(page).fill(typed)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()
})
