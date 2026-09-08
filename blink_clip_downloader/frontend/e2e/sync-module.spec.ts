import { test, expect } from './coverage-fixtures'

// scripts/standalone_server.py's _FakeSyncModule: one sync module ("Home",
// serial E2E-SYNC-0001, firmware 2.13.30) with the same three cameras every
// other tab's fake data uses (Front Door, Backyard, Garage) -- Garage
// starts offline and Backyard starts with a low battery, everything else
// starts armed, matching this suite's general "seed a few interesting
// states rather than all-identical fixtures" convention.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
})

test('shows the sync module info and every one of its cameras', async ({ page }) => {
  // Library's own nav (always mounted, never v-if-gated) also lists every
  // camera name in its sidebar filter regardless of which tab is active, so
  // scope every assertion here to this tab's own page container.
  const tab = page.locator('#page-syncmodule')
  await expect(tab.getByText('Home', { exact: true })).toBeVisible()
  await expect(tab.getByText('Firmware 2.13.30')).toBeVisible()
  await expect(tab.getByText('Serial E2E-SYNC-0001')).toBeVisible()
  for (const camera of ['Front Door', 'Backyard', 'Garage']) {
    await expect(tab.getByText(camera, { exact: true })).toBeVisible()
  }
  await expect(tab.getByText('Low battery')).toBeVisible()
  const garageCard = tab.locator('.sm-cam-card', { hasText: 'Garage' })
  await expect(garageCard.getByText('Offline', { exact: true })).toBeVisible()
})

test('starts with the system fully armed', async ({ page }) => {
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-armed/)
})

test('toggling one camera off switches the system to partially armed, and re-arming it restores fully armed', async ({
  page,
}) => {
  const frontDoorCard = page.locator('.sm-cam-card', { hasText: 'Front Door' })
  await frontDoorCard.locator('input[role="switch"]').click()
  await expect(frontDoorCard.getByText('Disarmed', { exact: true })).toBeVisible()
  await expect(page.getByText('Front Door disarmed')).toBeVisible()

  // The sync module itself is still armed, but with one of its cameras now
  // disarmed the system as a whole is no longer fully protected -- a real,
  // meaningful difference the headline status must surface.
  await expect(page.locator('.system-hero-title')).toHaveText('Partially Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-mixed/)
  await expect(page.getByText('2 of 3 cameras armed')).toBeVisible()

  // Re-arming the camera brings every camera back to armed, so the system
  // as a whole must go back to fully "System Armed" too.
  await frontDoorCard.locator('input[role="switch"]').click()
  await expect(frontDoorCard.getByText('Armed', { exact: true })).toBeVisible()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-armed/)
})

test('disarming the entire system requires confirmation, and does nothing if declined', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})

test('pressing Escape on the disarm confirmation also declines, same as Cancel', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Confirm' })).toHaveCount(0)
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})

test('disarming and re-arming the entire system via the hero button', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('Disarmed')
  await expect(page.getByText('Entire system disarmed')).toBeVisible()
  await expect(page.locator('.sync-module-card').getByText('Disarmed', { exact: true }).first()).toBeVisible()

  // Re-arming needs no confirmation.
  await page.getByRole('button', { name: 'Arm Entire System' }).click()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.getByText('Entire system armed')).toBeVisible()
})

test("toggling the sync module's own switch also asks for confirmation before disarming", async ({ page }) => {
  const homeCard = page.locator('.sync-module-card', { hasText: 'Home' })
  await homeCard.locator('.sm-module-arm input[role="switch"]').click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('Disarmed')

  // Leave it armed again for any later test/run against this same backend.
  await homeCard.locator('.sm-module-arm input[role="switch"]').click()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})
