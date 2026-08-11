import { test, expect } from '@playwright/test'

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
