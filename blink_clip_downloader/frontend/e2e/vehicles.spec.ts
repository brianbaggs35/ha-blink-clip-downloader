import { test, expect } from './coverage-fixtures'

// The Vehicles tab's camera list comes from the DB's distinct cameras
// (get_camera_stats(), same source /api/cameras uses for the Library
// sidebar), not a live Blink connection — so it's fully exercisable
// against the seeded clips even standalone.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')
})

test('lists every seeded camera as its own card', async ({ page }) => {
  for (const camera of ['Front Door', 'Backyard', 'Garage', 'Test Scratch']) {
    await expect(page.getByText(`📷 ${camera}`, { exact: true })).toBeVisible()
  }
})

test('saving the protected vehicle description persists after reload', async ({ page }) => {
  const description = 'e2e test vehicle: silver sedan, parked nose-in'
  await page.locator('#vehicle-description').fill(description)
  await page.getByRole('button', { name: 'Save Description' }).click()
  await expect(page.getByText('Protected vehicle description saved')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await expect(page.locator('#vehicle-description')).toHaveValue(description)
})

test('marking a camera as a car camera reveals the zone picker and persists after reload', async ({ page }) => {
  const card = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await expect(card.locator('.zone-picker')).toHaveCount(0)

  await card.locator('input[role="switch"]').click()
  await expect(card.locator('.zone-picker')).toBeVisible()

  await page.getByRole('button', { name: 'Save Camera Settings' }).click()
  await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  const cardAfterReload = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await expect(cardAfterReload.locator('input[role="switch"]')).toBeChecked()
  await expect(cardAfterReload.locator('.zone-picker')).toBeVisible()
})

// Continues from the previous test: Test Scratch is already a car camera
// with its zone picker showing (page was just reloaded, so it's back in
// "edit" mode — no zone saved yet). The picker's background image is a
// real, ffmpeg-generated thumbnail (standalone_server.py's
// e2e-biometrics-source clip, the newest on this camera, auto-selected) —
// unlike a placeholder file, its real <img> load event actually fires, so
// the drawing surface initializes with a real, non-zero bounding box.
test('drawing and saving a rectangle zone shows the saved preview and survives a reload', async ({ page }) => {
  const card = page.locator('.camera-card', { hasText: 'Test Scratch' })
  // boundingBox() doesn't itself wait for the background <img> to actually
  // finish loading (only for the element to be attached) — the overlay's
  // size comes entirely from that image's natural size, so measuring too
  // early can catch it still at its pre-load, zero-height layout. Under a
  // fast/idle server this raced and "worked" anyway; under the full suite's
  // cumulative load it doesn't reliably. Poll for a real decoded image
  // first instead of trusting a single early measurement.
  const img = card.locator('.picker-image')
  await expect(img).toBeVisible()
  await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)

  const overlay = card.locator('.picker-overlay')
  // page.mouse works in raw viewport coordinates and does not auto-scroll
  // the way locator actions do — Test Scratch (last of 4 camera cards) can
  // sit below the default viewport, so boundingBox() coordinates without
  // this landed off-screen and the "drag" hit nothing.
  await overlay.scrollIntoViewIfNeeded()
  const box = await overlay.boundingBox()
  if (!box) throw new Error('zone picker overlay has no bounding box — background image did not load')

  await page.mouse.move(box.x + 20, box.y + 20)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width - 20, box.y + box.height - 20, { steps: 5 })
  await page.mouse.up()
  await expect(card.locator('.zone-rect')).toBeVisible()

  const saveZoneBtn = card.getByRole('button', { name: 'Save zone' })
  await expect(saveZoneBtn).toBeEnabled()
  await saveZoneBtn.click()
  await expect(page.getByText('Vehicle zone saved')).toBeVisible()

  // Saving switches to the frozen preview: the interactive drawing overlay
  // is replaced by a static one (same .picker-overlay class, plus a
  // .picker-overlay-static modifier — not a different element) showing the
  // saved rectangle with no resize handles.
  await expect(card.locator('.picker-overlay-static')).toBeVisible()
  await expect(card.locator('.zone-handle')).toHaveCount(0)
  await expect(card.locator('.zone-rect')).toBeVisible()
  await expect(card.getByRole('button', { name: 'Edit zone' })).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  const cardAfterReload = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await expect(cardAfterReload.locator('.zone-rect')).toBeVisible()
})

test('Edit zone re-enters the drawing view, and Clear zone removes the saved zone', async ({ page }) => {
  const card = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await card.getByRole('button', { name: 'Edit zone' }).click()
  await expect(card.locator('.picker-overlay')).toBeVisible()
  await expect(card.getByRole('button', { name: 'Edit zone' })).toHaveCount(0)

  // Back out without saving, then clear the zone from the preview instead.
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  const cardAfterReload = page.locator('.camera-card', { hasText: 'Test Scratch' })
  await cardAfterReload.getByRole('button', { name: 'Clear zone' }).click()
  await expect(page.getByText('Clear the protected-vehicle zone for "Test Scratch"?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Vehicle zone cleared')).toBeVisible()
  await expect(cardAfterReload.locator('.zone-rect')).toHaveCount(0)
  await expect(cardAfterReload.locator('.picker-overlay')).toBeVisible()
})
