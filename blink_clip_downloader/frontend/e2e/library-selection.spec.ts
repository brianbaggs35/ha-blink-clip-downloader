import { test, expect } from './coverage-fixtures'

// Runs after library-filters.spec.ts and library-modal.spec.ts
// (alphabetically, and therefore chronologically given workers: 1): bulk
// star below mutates two distribution clips' starred state, which would
// throw off library-filters.spec.ts's exact "3 pre-starred" count if it
// ran first. status.spec.ts's own "Starred" stat is already documented as
// order-dependent for the same reason (library-modal.spec.ts stars one
// more elsewhere), so this adds to an already-flexible assertion, not a
// fixed one.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('sort order and date range selectors change which clips load', async ({ page }) => {
  await page.locator('#sort-order').click()
  await page.getByRole('option', { name: '⬇ Oldest' }).click()
  const firstIdOldest = await page.locator('.clip-card').first().getAttribute('data-id')

  await page.locator('#sort-order').click()
  await page.getByRole('option', { name: '⬆ Newest' }).click()
  const firstIdNewest = await page.locator('.clip-card').first().getAttribute('data-id')

  // The distribution clips span 0-11 hours old, so the oldest- and
  // newest-first views must disagree on which clip comes first.
  expect(firstIdOldest).not.toBe(firstIdNewest)

  await page.locator('#date-range').click()
  await page.getByRole('option', { name: 'Today' }).click()
  await expect(page.locator('.clip-card')).not.toHaveCount(0)
})

test('selecting a clip checkbox enters select mode and shows a live count', async ({ page }) => {
  await expect(page.locator('#bulk-bar')).toHaveCount(0)

  const first = page.locator('.clip-card[data-id="e2e-clip-002"]')
  await first.locator('.sel-check').click()
  await expect(page.locator('#bulk-bar')).toBeVisible()
  await expect(page.locator('#sel-count')).toHaveText('1 selected')
  await expect(first).toHaveClass(/selected/)

  const second = page.locator('.clip-card[data-id="e2e-clip-005"]')
  await second.locator('.sel-check').click()
  await expect(page.locator('#sel-count')).toHaveText('2 selected')

  // Unchecking goes back down, not just up.
  await first.locator('.sel-check').click()
  await expect(page.locator('#sel-count')).toHaveText('1 selected')

  await page.getByRole('button', { name: '✕ Cancel' }).click()
  await expect(page.locator('#bulk-bar')).toHaveCount(0)
  await expect(second).not.toHaveClass(/selected/)
})

test('Select all selects every currently loaded clip', async ({ page }) => {
  await page.getByRole('button', { name: 'Select', exact: true }).click()
  await expect(page.locator('#bulk-bar')).toBeVisible()
  await expect(page.locator('#sel-count')).toHaveText('0 selected')

  const total = await page.locator('.clip-card').count()
  await page.getByRole('button', { name: `Select all ${total}` }).click()
  await expect(page.locator('#sel-count')).toHaveText(`${total} selected`)
  await expect(page.locator('.clip-card.selected')).toHaveCount(total)

  await page.getByRole('button', { name: '✕ Cancel' }).click()
})

test('bulk-starring selected clips shows a confirmation toast and star badges', async ({ page }) => {
  const first = page.locator('.clip-card[data-id="e2e-clip-002"]')
  const second = page.locator('.clip-card[data-id="e2e-clip-005"]')
  await first.locator('.sel-check').click()
  await second.locator('.sel-check').click()

  await page.getByRole('button', { name: '★ Star selected' }).click()
  await expect(page.getByText('Starred 2 clip(s)')).toBeVisible()

  // bulkStar() closes select mode and refetches — both clips now carry a
  // star badge in the plain (non-select-mode) grid.
  await expect(page.locator('#bulk-bar')).toHaveCount(0)
  await expect(first.locator('.star-badge')).toBeVisible()
  await expect(second.locator('.star-badge')).toBeVisible()
})

test('bulk ZIP export downloads the selected clips', async ({ page }) => {
  // e2e-biometrics-source specifically: _handle_export_zip only includes a
  // clip if its file_path exists on disk and 404s if none of the selection
  // does — true of every *distribution* clip (no real file backs them),
  // but this one is a real, ffmpeg-generated file (see
  // standalone_server.py), so this exercises the actual success path
  // (blob download), not just the "ZIP export failed" fallback toast.
  await page.locator('.clip-card[data-id="e2e-biometrics-source"]').locator('.sel-check').click()

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '⬇ ZIP' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('blink-clips.zip')
  await expect(page.getByText('Downloaded 1 clip(s) as ZIP')).toBeVisible()
})

test('bulk ZIP export reports failure when none of the selected clips have a file on disk', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-007"]').locator('.sel-check').click()
  await page.getByRole('button', { name: '⬇ ZIP' }).click()
  await expect(page.getByText('ZIP export failed')).toBeVisible()
})

test('bulk-analyzing selected clips runs a real analysis on each and reports how many completed', async ({ page }) => {
  // e2e-clip-008/009: distinct from every clip another spec file selects by
  // id (000/001 by library-modal.spec.ts and ai.spec.ts, 002/005 by the
  // bulk-star test above, 007 by the bulk-ZIP-failure test above, 003/004/006
  // pre-starred/tagged by standalone_server.py) — analyzing them can't
  // disturb any other test's exact-count assertion.
  const first = page.locator('.clip-card[data-id="e2e-clip-008"]')
  const second = page.locator('.clip-card[data-id="e2e-clip-009"]')
  await first.locator('.sel-check').click()
  await second.locator('.sel-check').click()

  await page.getByRole('button', { name: '🔬 Analyze selected' }).click()
  await expect(
    page.getByText('Analyze 2 clip(s) with AI? This uses real API tokens and may take a while.'),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  // Neither clip has a real file on disk, so the real (not mocked)
  // analyze_clip code path genuinely completes for both rather than
  // throwing — same deterministic "no frames" result library-modal.spec.ts's
  // AI panel test already exercises from a different entry point.
  await expect(page.getByText('Analyzed 2/2 clip(s)')).toBeVisible()
  await expect(page.locator('#bulk-bar')).toHaveCount(0)
})

test('bulk delete removes the selected clip after confirmation', async ({ page }) => {
  // e2e-scratch-delete: its own dedicated clip (standalone_server.py's
  // _DELETE_TEST_CLIP_ID) — deleting it can't shrink any other test's
  // exact-count assertion the way deleting a distribution or star/tag
  // scratch clip would.
  const card = page.locator('.clip-card[data-id="e2e-scratch-delete"]')
  await expect(card).toBeVisible()
  await card.locator('.sel-check').click()

  await page.getByRole('button', { name: '🗑 Delete selected' }).click()
  await expect(page.getByText('Delete 1 clip(s) permanently?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Deleted 1 clip(s)')).toBeVisible()
  await expect(page.locator('#bulk-bar')).toHaveCount(0)
  await expect(page.locator('.clip-card[data-id="e2e-scratch-delete"]')).toHaveCount(0)
})
