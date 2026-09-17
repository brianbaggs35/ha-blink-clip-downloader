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
  // 4, not 3: list_camera_names() (standalone_server.py) also includes
  // Test Scratch alongside the 3 "distribution" cameras, so it can be
  // recognized as a live camera by /api/ai/camera-configs too.
  await expect(tiles).toHaveCount(4)
  await expect(tiles.filter({ hasText: 'Front Door' })).toBeVisible()
  await expect(tiles.filter({ hasText: 'Backyard' })).toBeVisible()
  await expect(tiles.filter({ hasText: 'Garage' })).toBeVisible()
  await expect(tiles.filter({ hasText: 'Test Scratch' })).toBeVisible()
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

test('a Save that the backend refuses says so instead of looking like it worked', async ({ page }) => {
  // Routed to a 500, so this one writes nothing — it deliberately sits
  // above the mutating test below and must leave the shared settings file
  // exactly as it found it.
  await page.route('**/api/security-feed/settings', (route) =>
    route.request().method() === 'PUT' ? route.fulfill({ status: 500, body: 'boom' }) : route.continue(),
  )
  await page.getByRole('button', { name: 'Customize' }).click()
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page.getByText('Could not save Security Feed settings')).toBeVisible()
  await expect(page.locator('.secfeed-tile')).toHaveCount(4)
})

// Mutates the shared security_feed_settings.json (a real PUT + file write,
// same file every test in this run shares) — must run last, after every
// assertion above that depends on all three cameras being displayed at the
// default (unfiltered) settings. Declaration order is execution order here
// (workers: 1, no intra-file parallelism), matching storage.spec.ts's own
test('a feed that cannot load says so instead of showing an empty grid', async ({ page }) => {
  // Only the *save* failure was covered before; a failed initial load took
  // a different path (loadError) that nothing exercised, and getting it
  // wrong shows an empty page rather than an explanation.
  await page.route('**/api/security-feed/cameras', (route) => route.fulfill({ status: 500, body: 'boom' }))
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="securityfeed"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="securityfeed"]')

  await expect(page.getByText('Failed to load the Security Feed')).toBeVisible()
  await expect(page.locator('.secfeed-tile')).toHaveCount(0)
  await page.unroute('**/api/security-feed/cameras')
})

test('a cross-tab Refresh re-reads the feed', async ({ page }) => {
  let fetches = 0
  await page.route('**/api/security-feed/cameras', (route) => {
    fetches += 1
    return route.continue()
  })
  // Settles the mount-time load before counting the refresh's own fetch.
  await expect(page.locator('.secfeed-tile').first()).toBeVisible()
  const afterMount = fetches

  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => fetches).toBeGreaterThan(afterMount)
})

test('a cross-tab Refresh leaves unsaved Customize edits alone', async ({ page }) => {
  // The other half of the same watcher, and the half worth having: a
  // refresh signal from another tab must not silently reload settings out
  // from under someone part-way through editing them.
  await page.getByRole('button', { name: 'Customize' }).click()
  const interval = page.getByRole('spinbutton')
  await interval.fill('45')

  let fetches = 0
  await page.route('**/api/security-feed/cameras', (route) => {
    fetches += 1
    return route.continue()
  })
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()

  // Nothing refetched, and the in-progress edit is still on screen.
  await expect(interval).toHaveValue('45')
  expect(fetches).toBe(0)
})

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
