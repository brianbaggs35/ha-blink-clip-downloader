import { test, expect } from './coverage-fixtures'

// Every card on this tab is backed by the DB/analyzer state the
// standalone server sets up, no live Blink connection needed — except
// "today" sub-counts (the seeded clips' timestamps are N hours before
// whenever the test suite happens to run, so how many fall in "today"
// depends on the wall-clock time/timezone of the run itself) and
// "Starred" (library-modal.spec.ts stars one more clip elsewhere in this
// same run — with workers: 1 sharing one backend for the whole suite,
// that's real, order-dependent state, not a stale assumption; the exact
// starred count is already covered in isolation by library-filters.spec.ts).
// Assertions here stick to what's true regardless of when/in what order
// this runs, with two order-dependent exceptions, both only valid because
// status.spec.ts alphabetically (and therefore chronologically, given
// workers: 1) runs before storage.spec.ts:
// - Archived starts at a fixed seed count of 3, but storage.spec.ts's
//   delete test removes one of them later in the run.
// - Total clips/Test Scratch's count include standalone_server.py's
//   _PENDING_ARCHIVE_CLIP_ID and _BIOMETRICS_CLIP_ID (both archived=FALSE-
//   filtered, so they count here, same as every other not-yet-archived
//   clip) — storage.spec.ts's own "Run Archiving Now" test archives the
//   former away later in the run, which is exactly what that test is
//   verifying; the latter stays archived=FALSE for the whole run.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')
})

test('shows disconnected (no live Blink session) and the seeded library totals', async ({ page }) => {
  const connectionCard = page.locator('.status-card', { hasText: 'Blink Connection' })
  await expect(connectionCard.getByText('Disconnected')).toBeVisible()

  const libraryCard = page.locator('.status-card', { hasText: 'Clip Library' })
  await expect(libraryCard).toContainText('Total clips')
  await expect(libraryCard.locator('.status-row', { hasText: 'Total clips' })).toContainText('17')
  await expect(libraryCard.locator('.status-row', { hasText: 'Archived' })).toContainText('3')
})

test('shows every seeded camera with its total clip count', async ({ page }) => {
  const camerasCard = page.locator('.status-card', { hasText: 'Cameras (4)' })
  await expect(camerasCard).toBeVisible()
  await expect(camerasCard.locator('.status-row', { hasText: 'Front Door' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Backyard' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Garage' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Test Scratch' })).toContainText('5 clips')
})

test('shows the configured (but unreachable) AI provider as offline', async ({ page }) => {
  const aiCard = page.locator('.status-card', { hasText: 'AI Analysis' })
  await expect(aiCard.getByText('Offline')).toBeVisible()
  await expect(aiCard.locator('.status-row', { hasText: 'Provider' })).toContainText('Ollama (Local/LAN)')
  await expect(aiCard.locator('.status-row', { hasText: 'Model' })).toContainText('llava')
})

test('clicking an activity chart bar switches to the Library tab filtered to that date', async ({ page }) => {
  const firstRow = page.locator('.act-row').first()
  await expect(firstRow).toBeVisible()
  await firstRow.locator('.act-bar-wrap').click()

  await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
})

// Battery readings are seeded directly (add_battery_reading), independent
// of any clip — Front Door ends up "ok", Backyard ends up "low" with one
// prior recovered episode (see standalone_server.py's _seed).
test('shows a battery tile per camera with a recorded reading, distinguishing low from normal', async ({ page }) => {
  const strip = page.locator('#battery-strip')
  await expect(strip).toBeVisible()

  const frontDoor = strip.locator('.battery-tile', { hasText: 'Front Door' })
  await expect(frontDoor).toContainText('Normal')
  await expect(frontDoor).toContainText('1.65V')
  await expect(frontDoor).not.toHaveClass(/low/)

  const backyard = strip.locator('.battery-tile', { hasText: 'Backyard' })
  await expect(backyard).toContainText('Low')
  await expect(backyard).toHaveClass(/low/)
})

test('the battery strip renders above the rest of the Status tab', async ({ page }) => {
  const strip = page.locator('#battery-strip')
  const grid = page.locator('#status-grid')
  const [stripBox, gridBox] = await Promise.all([strip.boundingBox(), grid.boundingBox()])
  expect(stripBox).not.toBeNull()
  expect(gridBox).not.toBeNull()
  expect(stripBox!.y).toBeLessThan(gridBox!.y)
})

test('clicking a battery tile opens its history with state transitions and how long it stayed low', async ({
  page,
}) => {
  await page.locator('#battery-strip .battery-tile', { hasText: 'Backyard' }).click()

  const dialog = page.getByRole('dialog').filter({ hasText: 'Backyard — Battery History' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('Went low')
  await expect(dialog).toContainText('Back to normal')

  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).not.toBeVisible()
})
