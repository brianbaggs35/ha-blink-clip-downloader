import type { Page } from '@playwright/test'
import { appApiUrl, test, expect } from './coverage-fixtures'
import { SCRATCH_CLIP, scratchClipBackup } from './backup-fixtures'

// Restoring through the real page against the real add-on: choose a file,
// look at what it would do, confirm, read what was done. The backup is built
// here around one clip the seeded library has never heard of, so adding it is
// visible and taking it away again leaves every other spec's data as it was.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="backup"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="backup"]')
})

test.afterEach(async ({ page }) => {
  // Over HTTP, not the page: a navigation here would throw away the
  // coverage the test just collected.
  await page.request.delete(appApiUrl(`/api/clips/${SCRATCH_CLIP}`))
})

async function choose(page: Page, buffer: Buffer, name = 'restore-me.zip') {
  await page.locator('#page-backup input[type="file"]').setInputFiles({ name, mimeType: 'application/zip', buffer })
}

async function clipRecord(page: Page) {
  return page.request.get(appApiUrl(`/api/clips/${SCRATCH_CLIP}`))
}

test('shows what a backup would do before anything is changed', async ({ page }) => {
  await choose(page, scratchClipBackup())

  const summary = page.locator('.restore-summary')
  await expect(summary).toContainText('What restoring will do')
  await expect(summary).toContainText('by version 6.1.4')
  await expect(summary).toContainText('All 2 parts checked against their checksums')
  // In the backup, will be added, already here.
  const clips = summary.locator('tr[data-table="clips"] td')
  await expect(clips).toHaveText(['1', '1', '0'])
  await expect(summary.locator('tr[data-table="analysis_results"] td')).toHaveText(['1', '1', '0'])
  await expect(summary.locator('.clip-notes')).toContainText('1 clip with no video on this device')
  await expect(summary.locator('.clip-notes')).toContainText('/share/blink-clips')
  await expect(summary.locator('.total-line')).toContainText(
    '2 records will be added, 1 clip of them. Nothing that is already here is overwritten or deleted.',
  )

  // Looking is not doing.
  expect((await clipRecord(page)).status()).toBe(404)
})

test('restores after a confirmation, then says what was done and checks it', async ({ page }) => {
  await choose(page, scratchClipBackup())
  await expect(page.locator('.restore-summary')).toBeVisible()

  await page.getByRole('button', { name: 'Restore this backup' }).click()
  const dialog = page.getByRole('dialog', { name: 'Restore this backup?' })
  await expect(dialog).toContainText(
    'This adds 2 records to this install, 1 clip of them. Nothing that is already here is overwritten or deleted.',
  )
  // Nothing has happened yet: the question has to be answered.
  expect((await clipRecord(page)).status()).toBe(404)
  await dialog.getByRole('button', { name: 'Confirm' }).click()

  const report = page.locator('.restore-report')
  await expect(report.locator('.report-verdict')).toHaveText('Restore complete and verified — 2 records added.')
  await expect(report.locator('.check-list li')).toHaveCount(3)
  await expect(report.locator('.check-list li.failed')).toHaveCount(0)
  await expect(report.locator('tr[data-table="clips"] td')).toHaveText(['1', '0'])

  // And it is really there, with its record intact and its video flagged as
  // not on this device (the flag that keeps the archiver from erasing it).
  const clip = await (await clipRecord(page)).json()
  expect(clip).toMatchObject({ id: SCRATCH_CLIP, camera: 'Restore Scratch Cam', starred: true, tags: ['restored'] })
  expect(clip.media_missing).toBe(true)
})

test('restoring the same backup again adds nothing and creates no duplicates', async ({ page }) => {
  await choose(page, scratchClipBackup())
  await page.getByRole('button', { name: 'Restore this backup' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm' }).click()
  await expect(page.locator('.report-verdict')).toContainText('2 records added')

  await page.getByRole('button', { name: 'Restore another backup' }).click()
  await choose(page, scratchClipBackup())

  const summary = page.locator('.restore-summary')
  await expect(summary.locator('.nothing-to-add')).toContainText('Everything in this backup is already here')
  await expect(summary.locator('tr[data-table="clips"] td')).toHaveText(['1', '0', '1'])
  await expect(summary.locator('.clip-notes')).toContainText(
    '1 clip already in this library — left exactly as they are.',
  )

  const listing = await page.request.get(appApiUrl(`/api/clips?search=${SCRATCH_CLIP}`))
  expect(((await listing.json()) as unknown[]).length).toBe(1)
})

test('a backup of this very install says there is nothing left to add', async ({ page }) => {
  const made = await page.request.post(appApiUrl('/api/backup/create'), { data: { include_faces: true } })
  expect(made.ok()).toBe(true)
  await choose(page, await made.body(), 'this-install.zip')

  const summary = page.locator('.restore-summary')
  await expect(summary).toContainText('What restoring will do')
  // Every table: nothing to add, whatever the library holds.
  const added = summary.locator('tbody tr td:nth-child(3)')
  await expect(added.first()).toBeVisible()
  for (const cell of await added.all()) await expect(cell).toHaveText('0')

  await page.getByRole('button', { name: 'Restore this backup' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm' }).click()
  await expect(page.locator('.report-verdict')).toHaveText('Restore complete and verified — 0 records added.')
})

test('declining the confirmation restores nothing', async ({ page }) => {
  let restoreRequests = 0
  await page.route('**/api/backup/uploads/*/restore', (route) => {
    restoreRequests++
    return route.continue()
  })
  await choose(page, scratchClipBackup())
  await page.getByRole('button', { name: 'Restore this backup' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click()

  await expect(page.locator('.restore-summary')).toBeVisible()
  expect(restoreRequests).toBe(0)
  expect((await clipRecord(page)).status()).toBe(404)
})

test('cancelling from the summary puts everything back to the start', async ({ page }) => {
  await choose(page, scratchClipBackup())
  await page.locator('.restore-actions').getByRole('button', { name: 'Cancel' }).click()

  await expect(page.locator('.restore-summary')).toHaveCount(0)
  await expect(page.locator('.restore-choose')).toBeVisible()
  expect((await clipRecord(page)).status()).toBe(404)
})

test('a restore that fails says so, and keeps the summary to try again', async ({ page }) => {
  await page.route('**/api/backup/uploads/*/restore', (route) =>
    route.fulfill({ status: 500, contentType: 'text/plain', body: 'The restore failed. Nothing was changed.' }),
  )
  await choose(page, scratchClipBackup())
  await page.getByRole('button', { name: 'Restore this backup' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm' }).click()

  await expect(page.locator('.restore-error')).toHaveText('The restore failed. Nothing was changed.')
  await expect(page.locator('.restore-summary')).toBeVisible()
})
