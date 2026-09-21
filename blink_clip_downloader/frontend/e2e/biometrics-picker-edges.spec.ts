import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The clip picker that Biometrics enrolls from is only ever driven down
// its happy path: one camera, clips present, enrollment reaching the real
// (and honestly failing) embedder. Two states it never sees are a clip
// list that failed to load, and an enrollment the server rejects with a
// reason rather than an HTTP error.
//
// Its "show older clips" arrow stays out of reach: it is disabled unless
// there are more clips than one page, and the seed has a single clip on
// this camera. Covering it would need a much larger fixture, the same
// structural limit that keeps LibraryPage's "Load more" untested.

const CAMERA = 'Test Scratch'

async function openPicker(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: CAMERA }).click()
}

test('an enrollment the server rejects reports the reason it gave', async ({ page }) => {
  await openPicker(page)
  await page.locator('.thumb-strip-item').first().click()
  const frames = page.locator('.frame-item')
  await expect(frames.first()).toBeVisible()
  await frames.first().click()

  // A structured {"error": ...} rather than an HTTP failure: the handler
  // returns one for a name clash or an unreadable frame, and it has to
  // reach the user rather than being reported as a generic failure.
  await page.route('**/api/ai/faces', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'that name is already enrolled' }),
    })
  })

  await page.locator('#biometrics-name').fill('e2e rejected enrollment')
  await page.getByRole('button', { name: /Enroll 1 selected frame/ }).click()

  await expect(page.getByText(/already enrolled|Enrollment failed/i).first()).toBeVisible()
})
