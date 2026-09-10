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
// - Archived starts at a fixed seed count of 5, but storage.spec.ts's
//   delete-entire-archive and single-clip-delete tests remove 3 of them
//   (2 then 1) later in the run.
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
  await expect(libraryCard.locator('.status-row', { hasText: 'Archived' })).toContainText('5')
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

  // Order-dependent, like "Starred" above: several earlier spec files (ai,
  // library-modal, library-selection) already ran real analyses by this
  // point in the suite, so the "Analyzed" row — hidden until
  // frameStats?.total_analyzed is truthy — now renders. Exact count isn't
  // asserted, only that it's showing something positive.
  const analyzedRow = aiCard.locator('.status-row', { hasText: 'Analyzed' })
  await expect(analyzedRow).toBeVisible()
  const analyzedText = (await analyzedRow.locator('.val').textContent()) ?? ''
  expect(Number(analyzedText)).toBeGreaterThan(0)
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

test('shows the Storage card with quota usage and a Frames Analyzed card when both are reported', async ({ page }) => {
  // stats.disk only ever comes from app.py's real poll loop (StorageManager.
  // disk_stats(), set on MediaServer.extra_status) -- standalone_server.py
  // never runs that loop, so this card is unreachable without patching the
  // real /api/stats response. Same story for analysis_stats.total_frames_analyzed
  // on /api/ai/status -- the real seeded analyzer never runs the frame-level
  // vision pipeline. beforeEach already navigated before this test's routes
  // exist, so a reload is needed to apply them.
  await page.route('**/api/stats', async (route) => {
    const response = await route.fetch()
    const stats = (await response.json()) as Record<string, unknown>
    await route.fulfill({
      response,
      json: {
        ...stats,
        disk: {
          used_bytes: 9_500_000_000,
          used_mb: 9500.0,
          free_bytes: 500_000_000,
          free_gb: 0.47,
          total_bytes: 10_000_000_000,
          total_gb: 9.31,
          quota_bytes: 10_000_000_000,
          quota_gb: 9.31,
        },
      },
    })
  })
  await page.route('**/api/ai/status', async (route) => {
    const response = await route.fetch()
    const status = (await response.json()) as { analysis_stats?: Record<string, unknown> }
    await route.fulfill({
      response,
      json: {
        ...status,
        analysis_stats: { ...status.analysis_stats, total_frames_analyzed: 42, frames_analyzed_today: 3 },
      },
    })
  })

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')

  const storageCard = page.locator('.status-card', { hasText: 'Storage' })
  await expect(storageCard.locator('.status-row', { hasText: 'Used' })).toContainText('9500')
  await expect(storageCard.locator('.status-row', { hasText: 'Quota' })).toContainText('9.31')
  // Usage is 95% of quota -- the "danger" (>90%) threshold, not just any non-null one.
  await expect(storageCard.locator('.val.danger')).toHaveCount(1)

  const framesCard = page.locator('.status-card', { hasText: 'Frames Analyzed' })
  await expect(framesCard.locator('.status-row', { hasText: 'Total frames' })).toContainText('42')
  await expect(framesCard.locator('.status-row', { hasText: 'Today' })).toContainText('3')
})

test('shows the warn disk threshold, and the AI queue pending / suspicious counts, when reported', async ({ page }) => {
  await page.route('**/api/stats', async (route) => {
    const response = await route.fetch()
    const stats = (await response.json()) as Record<string, unknown>
    await route.fulfill({
      response,
      json: {
        ...stats,
        disk: {
          used_bytes: 7_500_000_000,
          used_mb: 7500.0,
          free_bytes: 2_500_000_000,
          free_gb: 2.33,
          total_bytes: 10_000_000_000,
          total_gb: 9.31,
          quota_bytes: 10_000_000_000,
          quota_gb: 9.31,
        },
      },
    })
  })
  await page.route('**/api/ai/status', async (route) => {
    const response = await route.fetch()
    const status = (await response.json()) as {
      analysis_stats?: Record<string, unknown>
      queue?: Record<string, unknown>
    }
    await route.fulfill({
      response,
      json: {
        ...status,
        queue: { ...status.queue, pending: 3 },
        analysis_stats: { ...status.analysis_stats, suspicious_count: 2 },
      },
    })
  })

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')

  // 75% usage -- the "warn" (>70%, <=90%) threshold, distinct from "danger".
  const storageCard = page.locator('.status-card', { hasText: 'Storage' })
  await expect(storageCard.locator('.val.warn')).toHaveCount(1)
  await expect(storageCard.locator('.val.danger')).toHaveCount(0)

  const aiCard = page.locator('.status-card', { hasText: 'AI Analysis' })
  await expect(aiCard.locator('.status-row', { hasText: 'Pending' })).toContainText('3')
  await expect(aiCard.locator('.status-row', { hasText: 'Suspicious' })).toContainText('2')
})

test('shows the ok disk threshold when usage is comfortably under both warn and danger', async ({ page }) => {
  await page.route('**/api/stats', async (route) => {
    const response = await route.fetch()
    const stats = (await response.json()) as Record<string, unknown>
    await route.fulfill({
      response,
      json: {
        ...stats,
        disk: {
          used_bytes: 3_000_000_000,
          used_mb: 3000.0,
          free_bytes: 7_000_000_000,
          free_gb: 6.52,
          total_bytes: 10_000_000_000,
          total_gb: 9.31,
          quota_bytes: 10_000_000_000,
          quota_gb: 9.31,
        },
      },
    })
  })

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')

  const storageCard = page.locator('.status-card', { hasText: 'Storage' })
  await expect(storageCard.locator('.val.ok')).toHaveCount(1)
  await expect(storageCard.locator('.val.warn')).toHaveCount(0)
  await expect(storageCard.locator('.val.danger')).toHaveCount(0)
})

test('shows an error message when the status data fails to load', async ({ page }) => {
  await page.route('**/api/stats', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  // Same trigger as storage.spec.ts's equivalent test: the initial load
  // already succeeded during beforeEach, so Refresh is what re-runs load()
  // against the now-mocked-failing endpoint.
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.getByText('Failed to load status.')).toBeVisible()
})
