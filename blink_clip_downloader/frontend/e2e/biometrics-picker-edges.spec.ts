import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The failure paths of Biometrics' enroll flow: a clip list that won't load,
// a clip deleted before its turn to be scanned, a photo that isn't one, and
// an enrollment the server turns down. Each has to reach the user as a
// reason, not a raw status line or silence. Mutates nothing lasting —
// biometrics.spec.ts (which runs after this file) asserts the seeded people.

async function openBiometrics(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
}

async function chooseScratchCamera(page: Page) {
  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: 'Test Scratch', exact: true }).click()
}

test('a clip list that fails to load offers a retry', async ({ page }) => {
  await page.route(/\/api\/clips\?/, (route) => route.fulfill({ status: 500, body: 'boom' }))
  await openBiometrics(page)
  await expect(page.getByText("Couldn't load clips.")).toBeVisible()

  await page.unroute(/\/api\/clips\?/)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.locator('.clip-tile').first()).toBeVisible()
})

test('a clip deleted before its scan says so, and can be tried again', async ({ page }) => {
  await page.route('**/api/ai/faces/scan/*', (route) => route.fulfill({ status: 404, body: 'Clip not found' }))
  await openBiometrics(page)
  await chooseScratchCamera(page)
  const tile = page.locator('.clip-tile')
  await tile.click()
  await expect(tile).toContainText('Failed — tap to retry')
  // Shown on the tile, not only in a tooltip a phone cannot hover for.
  await expect(tile.locator('.clip-tile-error')).toHaveText('This clip is no longer in the library')
  await expect(tile).toHaveAttribute('title', 'This clip is no longer in the library')

  // A failure is retried by picking the clip again — here it scans for real.
  await page.unroute('**/api/ai/faces/scan/*')
  await tile.click()
  await expect(tile).toContainText('3 faces')
  await expect(tile.locator('.clip-tile-error')).toHaveCount(0)
})

test("a file that isn't a readable photo is explained", async ({ page }) => {
  // Real server, no mocking: the bytes reach face detection, which cannot
  // open them — the same path as an iPhone HEIC in a browser that can't
  // decode one.
  await openBiometrics(page)
  await page.getByRole('tab', { name: 'From a photo' }).click()
  await page.locator('input[type="file"]').setInputFiles({
    name: 'notes.jpg',
    mimeType: 'image/jpeg',
    buffer: Buffer.from('definitely not a jpeg'),
  })
  await expect(page.locator('.photo-results')).toContainText("This photo couldn't be read")
  await expect(page.locator('.face-tile')).toHaveCount(0)
})

test('an enrollment the server turns down reports the reason it gave', async ({ page }) => {
  await openBiometrics(page)
  await chooseScratchCamera(page)
  await page.locator('.clip-tile').click()
  await page.locator('.face-tile').first().click()

  await page.route('**/api/ai/faces', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    await route.fulfill({
      json: { error: 'Those faces are no longer available — scan again to pick them', enrolled: 0, expired: 1 },
    })
  })
  await page.locator('#biometrics-name').fill('e2e rejected enrollment')
  await page.getByRole('button', { name: 'Enroll as e2e rejected enrollment' }).click()
  await expect(page.getByText('Those faces are no longer available — scan again to pick them')).toBeVisible()
  await expect(page.locator('.person-card', { hasText: 'e2e rejected enrollment' })).toHaveCount(0)
})
