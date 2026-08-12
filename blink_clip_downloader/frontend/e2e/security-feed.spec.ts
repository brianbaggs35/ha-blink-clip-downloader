import { test, expect } from './coverage-fixtures'

// Unlocked in standalone_server.py via a fake list_camera_names plus a real
// (tiny, Pillow-generated) JPEG per camera from get_camera_snapshot — the
// same cheap "no real hardware needed" trick the AI tab uses with a real
// ClipAnalyzer pointed at an unreachable port. Garage deliberately returns
// no snapshot (None) so its tile exercises the "No snapshot available yet"
// placeholder path, not just the happy path every other camera takes.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="securityfeed"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="securityfeed"]')
})

test('renders a tile per camera with the info banner', async ({ page }) => {
  await expect(page.locator('.secfeed-info-banner')).toContainText('only change when Blink itself records new motion')
  const tiles = page.locator('.secfeed-tile')
  await expect(tiles).toHaveCount(3)
  await expect(tiles.filter({ hasText: 'Front Door' })).toBeVisible()
  await expect(tiles.filter({ hasText: 'Backyard' })).toBeVisible()
  await expect(tiles.filter({ hasText: 'Garage' })).toBeVisible()
})

test('shows a real snapshot for a camera with one cached, and the placeholder for one without', async ({ page }) => {
  const frontDoorImg = page.locator('.secfeed-tile', { hasText: 'Front Door' }).locator('img')
  await expect(frontDoorImg).toBeVisible()
  await expect(frontDoorImg).not.toHaveClass(/secfeed-tile-image-hidden/)

  const garageTile = page.locator('.secfeed-tile', { hasText: 'Garage' })
  await expect(garageTile.locator('img')).toHaveClass(/secfeed-tile-image-hidden/)
  await expect(garageTile.getByText('No snapshot available yet')).toBeVisible()
})

test('the Customize panel starts collapsed and expands on click', async ({ page }) => {
  await expect(page.locator('#secfeed-cameras')).not.toBeVisible()
  await page.getByRole('button', { name: 'Customize' }).click()
  await expect(page.locator('#secfeed-cameras')).toBeVisible()
})

// Mutates the shared security_feed_settings.json (a real PUT + file write,
// same file every test in this run shares) — must run last, after every
// assertion above that depends on all three cameras being displayed at the
// default (unfiltered) settings. Declaration order is execution order here
// (workers: 1, no intra-file parallelism), matching storage.spec.ts's own
// "mutating test goes last" convention.
test('saving Customize settings narrows the displayed cameras and persists across a reload', async ({ page }) => {
  await page.getByRole('button', { name: 'Customize' }).click()

  await page.locator('#secfeed-cameras').click()
  await page.getByRole('option', { name: 'Backyard' }).click()
  await page.keyboard.press('Escape')

  await page.getByRole('spinbutton').fill('20')
  await page.getByRole('button', { name: '1', exact: true }).click()
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page.getByText('Security Feed settings saved')).toBeVisible()
  const tiles = page.locator('.secfeed-tile')
  await expect(tiles).toHaveCount(1)
  await expect(tiles).toContainText('Backyard')
  await expect(page.locator('.secfeed-grid')).toHaveCSS('--secfeed-columns', '1')

  // A full reload re-fetches settings from the server instead of reusing
  // this component instance's in-memory state — proving the PUT actually
  // persisted (security_feed_settings.json), not just updated local state.
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="securityfeed"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="securityfeed"]')

  await expect(page.locator('.secfeed-tile')).toHaveCount(1)
  await expect(page.locator('.secfeed-tile')).toContainText('Backyard')
  await page.getByRole('button', { name: 'Customize' }).click()
  await expect(page.getByRole('spinbutton')).toHaveValue('20')
})
