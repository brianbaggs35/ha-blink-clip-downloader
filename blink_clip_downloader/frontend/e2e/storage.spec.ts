import { test, expect } from './coverage-fixtures'

// Archived Clips is fully DB-backed (standalone_server.py's _ARCHIVE_CLIPS
// seeds 3 pre-archived clips: two sharing one archive_path so they group
// into a single "archive", one alone in its own) — no Google account
// needed. Google Drive itself can't be exercised past its disconnected
// state without real OAuth credentials, so only that initial rendering is
// covered here.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
})

test('lists both seeded archives with their clip counts', async ({ page }) => {
  const panels = page.locator('.archive-panel')
  await expect(panels).toHaveCount(2)
  await expect(panels.filter({ hasText: '2024-01-e2e.zip' })).toContainText('2 clips')
  await expect(panels.filter({ hasText: '2024-02-e2e.zip' })).toContainText('1 clip')
})

test('expanding an archive groups its clips by camera', async ({ page }) => {
  const multiPanel = page.locator('.archive-panel', { hasText: '2024-01-e2e.zip' })
  await multiPanel.locator('.archive-panel-header').click()
  await expect(multiPanel.getByText('Front Door (1 clip)')).toBeVisible()
  await expect(multiPanel.getByText('Backyard (1 clip)')).toBeVisible()
  await expect(multiPanel.getByText('Not backed up')).toHaveCount(2)
})

test('filtering by camera narrows both the archive list and its counts', async ({ page }) => {
  await page.getByRole('combobox', { name: 'Filter by camera' }).click()
  await page.getByRole('option', { name: 'Front Door' }).click()

  // The Garage-only archive has no Front Door clips, so it drops out
  // entirely; the shared archive stays but its count narrows to just
  // the matching clip.
  const panels = page.locator('.archive-panel')
  await expect(panels).toHaveCount(1)
  await expect(panels.first()).toContainText('2024-01-e2e.zip')
  await expect(panels.first()).toContainText('1 clip')

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await expect(panels).toHaveCount(2)
})

test('deleting the only clip in an archive removes the whole group', async ({ page }) => {
  const soloPanel = page.locator('.archive-panel', { hasText: '2024-02-e2e.zip' })
  await soloPanel.locator('.archive-panel-header').click()
  await soloPanel.getByRole('button', { name: 'Delete' }).click()

  await expect(page.getByText('Delete this clip?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Clip deleted')).toBeVisible()
  await expect(page.locator('.archive-panel')).toHaveCount(1)
  await expect(page.getByText('2024-02-e2e.zip')).toHaveCount(0)
})

test('Google Drive card shows the disconnected setup prompt with no account configured', async ({ page }) => {
  const connectionCard = page.locator('.gdrive-connection-card')
  await expect(
    connectionCard.getByText('Set a Google OAuth Client ID and Secret above to connect an account.'),
  ).toBeVisible()
  await expect(page.locator('#gdrive-client-id')).toHaveValue('')
})

// Must run after every test above: it archives standalone_server.py's
// _PENDING_ARCHIVE_CLIP_ID (seeded old enough to already be eligible, on
// its own dedicated Test Scratch clip so it can't collide with anything
// else), which creates a brand new third archive group — the earlier
// tests in this file all assert an exact panel count (2, or 1 after the
// delete test) that a pre-existing archive group would throw off if this
// ran first. Declaration order is execution order here (workers: 1,
// no parallelism within a file).
test('Run Archiving Now sweeps the currently-eligible backlog immediately', async ({ page }) => {
  const panelsBefore = await page.locator('.archive-panel').count()

  await page.getByRole('button', { name: 'Run Archiving Now' }).click()
  await expect(page.getByText(/^Archived \d+ clip\(s\)$/)).toBeVisible()

  await expect(page.locator('.archive-panel')).toHaveCount(panelsBefore + 1)
})
