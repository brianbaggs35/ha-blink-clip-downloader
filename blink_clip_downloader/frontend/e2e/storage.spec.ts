import { test, expect } from './coverage-fixtures'

// Archived Clips is fully DB-backed (standalone_server.py's _ARCHIVE_CLIPS
// seeds 5 pre-archived clips across 3 archive_paths: two share one so they
// group into a single "archive", one is alone in its own (dedicated to the
// single-clip delete test), and two more share a third (dedicated to the
// delete-entire-archive test) — no Google account needed. Google Drive
// itself wires in a real (but uncredentialed)
// GDriveClient, so the settings form's save/persist round trip is real
// too — but actually connecting an account needs real OAuth credentials
// this environment doesn't have, so that (and anything nested behind it:
// quota, folder browsing, backup-now, GoogleDriveFailedUploads) stays out
// of reach here.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
})

test('lists all seeded archives with their clip counts', async ({ page }) => {
  const panels = page.locator('.archive-panel')
  await expect(panels).toHaveCount(3)
  await expect(panels.filter({ hasText: '2024-01-e2e.zip' })).toContainText('2 clips')
  await expect(panels.filter({ hasText: '2024-02-e2e.zip' })).toContainText('1 clip')
  await expect(panels.filter({ hasText: '2024-03-e2e.zip' })).toContainText('2 clips')
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
  await expect(panels).toHaveCount(3)
})

test('declining the delete-archive confirmation leaves the archive untouched', async ({ page }) => {
  const multiPanel = page.locator('.archive-panel', { hasText: '2024-01-e2e.zip' })
  await multiPanel.getByRole('button', { name: 'Delete archive' }).click()
  await expect(page.getByText('Delete archive?')).toBeVisible()

  await page.getByRole('button', { name: 'Cancel' }).click()

  await expect(page.getByText('Delete archive?')).toHaveCount(0)
  await expect(page.locator('.archive-panel')).toHaveCount(3)
  await expect(multiPanel).toContainText('2 clips')
})

test('shows an error message when archived clips fail to load', async ({ page }) => {
  await page.route('**/api/storage/archives*', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  // The initial load already happened (successfully) during beforeEach's
  // navigation -- Refresh is what re-runs loadGroups() against the
  // now-mocked-failing endpoint, same trigger app-sidebar.spec.ts uses for
  // its own "bumps the cross-tab refresh signal" test.
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.getByText('Failed to load archived clips.')).toBeVisible()
})

test('the camera filter dropdown degrades silently when its camera list fails to load', async ({ page }) => {
  await page.route('**/api/cameras', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  // loadCameras()'s own try/catch is independent of loadGroups() above --
  // archives still render fine, the camera filter just has only "All
  // cameras" to offer.
  await expect(page.locator('.archive-panel')).toHaveCount(3)
  await expect(page.getByRole('combobox', { name: 'Filter by camera' })).toContainText('All cameras')
})

test('a date range with no matching archives shows the filtered-empty state, and Clear filters restores the list', async ({
  page,
}) => {
  const panels = page.locator('.archive-panel')
  const today = new Date().toISOString().slice(0, 10)

  // Every seeded archived clip is over a week old, so filtering to just
  // today (since-only, "that calendar day" per archiveDateFilters' own
  // comment) matches nothing.
  await page.getByLabel('From date').fill(today)
  await expect(page.getByText('No archives match these filters.')).toBeVisible()
  await expect(panels).toHaveCount(0)

  // Adding an end date too (still today) exercises the since+until branch
  // of archiveDateFilters rather than the since-only fallback above.
  await page.getByLabel('To date').fill(today)
  await expect(page.getByText('No archives match these filters.')).toBeVisible()

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await expect(panels).toHaveCount(3)
})

// Deliberately reuses the shared 2024-01 archive (rather than a new
// dedicated fixture) specifically *because* it already has two clips on two
// different cameras -- this is the only remaining test that needs it intact
// beforehand, and nothing after this point in the file (or in any other
// spec file -- see standalone_server.py's _ARCHIVE_CLIPS comment) depends
// on 2024-01 still having both. Must run after the three tests above that
// do still need it at its original 2-clip state (list/expand/filter).
test('deleting one clip from a multi-clip archive keeps the group, decrementing its count', async ({ page }) => {
  const multiPanel = page.locator('.archive-panel', { hasText: '2024-01-e2e.zip' })
  await multiPanel.locator('.archive-panel-header').click()

  const deleteButtons = multiPanel.getByRole('button', { name: 'Delete', exact: true })
  await expect(deleteButtons).toHaveCount(2)
  // sortedForGrouping sorts by camera name ascending ("Backyard" < "Front
  // Door"), so the last row/button is Front Door's.
  await deleteButtons.last().click()

  await expect(page.getByText('Delete this clip?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Clip deleted')).toBeVisible()
  await expect(multiPanel).toContainText('1 clip')
  await expect(multiPanel.getByText('Front Door (1 clip)')).toHaveCount(0)
  await expect(multiPanel.getByText('Backyard (1 clip)')).toBeVisible()
  await expect(deleteButtons).toHaveCount(1)
  // The panel itself survives (unlike the solo-archive delete test below) --
  // one clip remains, so clip_count never reaches 0.
  await expect(page.locator('.archive-panel')).toHaveCount(3)
})

test('shows an error toast when Run Archiving Now fails', async ({ page }) => {
  // Mocked, so nothing actually archives -- doesn't disturb the panel
  // counts the tests below (and the real "Run Archiving Now" test at the
  // end of this file) depend on.
  await page.route('**/api/storage/archive/run-now', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  await page.getByRole('button', { name: 'Run Archiving Now' }).click()
  await expect(page.getByText('Could not run archiving')).toBeVisible()
})

// Runs before the solo-archive delete test below so that test's own "back
// down to 1 panel" assertion doesn't need to change to account for this
// archive too -- see _ARCHIVE_PATH_BULK_DELETE's comment in
// standalone_server.py. Deliberately deletes straight from the collapsed
// panel header, without expanding it first, since the button lives there
// rather than inside the (lazily-fetched) clip list.
test('deleting an entire archive removes every clip in it and the panel itself', async ({ page }) => {
  const bulkPanel = page.locator('.archive-panel', { hasText: '2024-03-e2e.zip' })
  await expect(bulkPanel).toContainText('2 clips')

  await bulkPanel.getByRole('button', { name: 'Delete archive' }).click()

  await expect(page.getByText('Delete archive?')).toBeVisible()
  await expect(page.getByText(/removes all 2 clips in 2024-03-e2e\.zip/)).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Archive deleted')).toBeVisible()
  await expect(page.locator('.archive-panel')).toHaveCount(2)
  await expect(page.getByText('2024-03-e2e.zip')).toHaveCount(0)

  // Confirms this was a real backend delete, not just an optimistic
  // client-side removal -- the archive stays gone after a fresh load.
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await expect(page.locator('.archive-panel')).toHaveCount(2)
  await expect(page.getByText('2024-03-e2e.zip')).toHaveCount(0)
})

test('deleting the only clip in an archive removes the whole group', async ({ page }) => {
  const soloPanel = page.locator('.archive-panel', { hasText: '2024-02-e2e.zip' })
  await soloPanel.locator('.archive-panel-header').click()
  // exact: true -- otherwise this also matches the archive-level "Delete
  // archive" button, since Playwright's role-name matching is substring by
  // default and "Delete archive" contains "Delete".
  await soloPanel.getByRole('button', { name: 'Delete', exact: true }).click()

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

test('the Google Drive setup help dialog explains the OAuth client steps', async ({ page }) => {
  await page.getByRole('button', { name: 'How to connect Google Drive' }).click()

  const helpDialog = page.getByRole('dialog', { name: 'Connecting Google Drive' })
  await expect(helpDialog).toBeVisible()
  await expect(helpDialog).toContainText('TVs and Limited Input devices')
  await expect(helpDialog.getByRole('link', { name: 'Google Cloud Console' })).toHaveAttribute(
    'href',
    'https://console.cloud.google.com/',
  )

  await page.keyboard.press('Escape')
  await expect(helpDialog).toHaveCount(0)
})

test('saving Google Drive settings persists them and survives a reload', async ({ page }) => {
  await page.locator('#gdrive-client-id').fill('123.apps.googleusercontent.com')
  await page.locator('#gdrive-client-secret input').fill('e2e-fake-secret')
  await page.locator('#gdrive-backup-policy').click()
  await page.getByRole('option', { name: 'All clips (archived + regular downloads)' }).click()

  await page.getByRole('button', { name: 'Save Setup' }).click()
  await expect(page.getByText('Google Drive settings saved')).toBeVisible()

  // The secret field clears itself right after a successful save (never
  // re-displayed once stored — see GoogleDriveCard.vue's saveSettings) but
  // the client id and backup policy both round-trip through a real reload.
  await expect(page.locator('#gdrive-client-secret input')).toHaveValue('')

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await expect(page.locator('#gdrive-client-id')).toHaveValue('123.apps.googleusercontent.com')
})

test('shows an error toast when saving Google Drive settings fails', async ({ page }) => {
  // Mocked, so the settings from the test above (or the seeded default)
  // are never actually overwritten -- doesn't disturb later tests.
  await page.route('**/api/storage/gdrive/settings', (route) => {
    if (route.request().method() !== 'PUT') return route.fallback()
    return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) })
  })
  await page.locator('#gdrive-client-id').fill('999.apps.googleusercontent.com')
  await page.getByRole('button', { name: 'Save Setup' }).click()
  await expect(page.getByText('Could not save Google Drive settings')).toBeVisible()
})

// Must run after every test above: it archives standalone_server.py's
// _PENDING_ARCHIVE_CLIP_ID (seeded old enough to already be eligible, on
// its own dedicated Test Scratch clip so it can't collide with anything
// else), which creates a brand new archive group — the earlier tests in
// this file all assert an exact panel count (3, then 2 after the delete-
// entire-archive test, then 1 after the solo-clip delete test) that a
// pre-existing archive group would throw off if this ran first.
// Declaration order is execution order here (workers: 1, no parallelism
// within a file).
test('Run Archiving Now sweeps the currently-eligible backlog immediately', async ({ page }) => {
  const panelsBefore = await page.locator('.archive-panel').count()

  await page.getByRole('button', { name: 'Run Archiving Now' }).click()
  await expect(page.getByText(/^Archived \d+ clip\(s\)$/)).toBeVisible()

  await expect(page.locator('.archive-panel')).toHaveCount(panelsBefore + 1)
})
