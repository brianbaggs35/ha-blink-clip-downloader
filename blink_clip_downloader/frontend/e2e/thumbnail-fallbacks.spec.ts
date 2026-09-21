import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// Four different places render a clip thumbnail, and every one of them has
// a fallback for the image failing to load — the Library grid, the
// Vehicles zone picker's strip, the Biometrics clip strip, and the
// Security Events timeline. None of those fallbacks had a test, which is
// the wrong way round: a thumbnail is the one asset most likely to be
// missing in real use, because it is generated separately from the clip
// and a failed ffmpeg run leaves the clip playable with no thumbnail at
// all. Without the fallback the user gets a broken-image icon and, in the
// Security timeline, a button that looks empty rather than absent.
//
// Routing the thumbnail endpoint to 404 is the honest way to provoke it:
// the <img> really fails, so the component's own @error handler runs
// rather than the state being set for it.

async function breakThumbnails(page: Page) {
  await page.route('**/api/clips/*/thumb*', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"error": "no thumbnail"}' }),
  )
}

async function gotoTab(page: Page, tab: string) {
  await page.locator(`.app-nav-tab[data-tab="${tab}"]`).click()
  await page.waitForSelector(`.app-nav-tab.active[data-tab="${tab}"]`)
}

test('the Library grid falls back to a placeholder rather than a broken image', async ({ page }) => {
  await breakThumbnails(page)
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')

  await expect(page.locator('.clip-card').first()).toBeVisible()
  // Every card that rendered swapped its <img> for the placeholder.
  await expect(page.locator('.clip-card .no-thumb').first()).toBeVisible()
  await expect(page.locator('.clip-card img')).toHaveCount(0)
})

test("the Vehicles zone picker's clip strip falls back per clip", async ({ page }) => {
  await breakThumbnails(page)
  await page.goto('/')
  await gotoTab(page, 'vehicles')

  const card = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await card.locator('input[role="switch"]').click()
  await expect(card.locator('.zone-picker')).toBeVisible()

  await expect(card.locator('.thumb-strip-item .no-thumb').first()).toBeVisible()

  // Leave the camera as the other Vehicles specs expect to find it.
  await card.locator('input[role="switch"]').click()
})

test('the Biometrics clip strip falls back per clip', async ({ page }) => {
  await breakThumbnails(page)
  await page.goto('/')
  await gotoTab(page, 'biometrics')

  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: 'Test Scratch' }).click()

  await expect(page.locator('.thumb-strip-item').first()).toBeVisible()
  await expect(page.locator('.thumb-strip-item .no-thumb').first()).toBeVisible()
})

test('the Security timeline drops the thumbnail button rather than showing an empty one', async ({ page }) => {
  await breakThumbnails(page)
  await page.goto('/')
  await gotoTab(page, 'security')

  const rows = page.locator('.security-card')
  await expect(rows.first()).toBeVisible()
  // Unlike the others this one removes the button entirely, since a
  // thumbnail button with nothing in it is not worth clicking.
  await expect(page.locator('.security-thumb')).toHaveCount(0)
})
