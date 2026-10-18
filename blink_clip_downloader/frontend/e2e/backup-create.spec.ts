import { readFileSync } from 'node:fs'
import { appApiUrl, test, expect } from './coverage-fixtures'

// Making a backup against the real, seeded add-on: what the page says a
// backup holds is what the API says, the download is a ZIP with the parts a
// restore needs, and the choice to leave faces out reaches the file. Nothing
// here changes the library, so these run beside every other spec.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="backup"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="backup"]')
  await expect(page.locator('.not-included')).toBeVisible()
})

async function previewTables(page: import('@playwright/test').Page) {
  const response = await page.request.get(appApiUrl('/api/backup/preview'))
  expect(response.ok()).toBe(true)
  return (await response.json()) as { tables: Array<{ name: string; rows: number; biometric: boolean }> }
}

test('lists what a backup would hold, as many as the library has', async ({ page }) => {
  const { tables } = await previewTables(page)
  const clips = tables.find((table) => table.name === 'clips')!
  expect(clips.rows).toBeGreaterThan(0)

  await expect(page.locator('.include-list [data-table="clips"] strong')).toHaveText(clips.rows.toLocaleString())
  // Nothing that is empty is listed.
  for (const table of tables.filter((t) => t.rows === 0)) {
    await expect(page.locator(`.include-list [data-table="${table.name}"]`)).toHaveCount(0)
  }
  await expect(page.locator('.not-included')).toContainText('every password and sign-in')
})

test('downloads a ZIP that holds the library and what a restore needs', async ({ page }) => {
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download backup' }).click(),
  ])

  expect(download.suggestedFilename()).toMatch(/^blink-clips-backup-\d{8}-\d{6}\.zip$/)
  const bytes = readFileSync(await download.path())
  // A ZIP, with the manifest and the clip records in it. Names in a ZIP's
  // directory are stored plain, whatever the contents are compressed to.
  expect(bytes.subarray(0, 4).toString('hex')).toBe('504b0304')
  expect(bytes.includes('manifest.json')).toBe(true)
  expect(bytes.includes('database/clips.jsonl')).toBe(true)
  await expect(page.locator('.backup-done')).toContainText('Downloaded blink-clips-backup-')
  await expect(page.locator('.backup-error')).toHaveCount(0)
})

test('never puts a password or sign-in in the backup', async ({ page }) => {
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download backup' }).click(),
  ])
  const bytes = readFileSync(await download.path())
  for (const secret of [
    'auth_credentials',
    'google_drive_credentials',
    'google_drive_settings',
    'web_access',
    'alert_action_key',
    'options.json',
  ]) {
    expect(bytes.includes(secret), secret).toBe(false)
  }
})

test('leaves the enrolled faces out of the file when asked', async ({ page }) => {
  const { tables } = await previewTables(page)
  test.skip(!tables.some((t) => t.biometric && t.rows > 0), 'nobody is enrolled here')

  await expect(page.locator('.include-list [data-table="face_enrollments"]')).toBeVisible()
  await expect(page.locator('.faces-option')).toContainText('never leave this device on their own')

  await page.locator('.faces-option .p-toggleswitch').click()
  await expect(page.locator('.include-list [data-table="face_enrollments"]')).toHaveCount(0)

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download backup' }).click(),
  ])
  const bytes = readFileSync(await download.path())
  expect(bytes.includes('database/clips.jsonl')).toBe(true)
  expect(bytes.includes('database/face_enrollments.jsonl')).toBe(false)
})

test('says why a backup could not be made, and carries on working', async ({ page }) => {
  await page.route('**/api/backup/create', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'text/plain',
      body: 'Creating the backup failed: No space left on device.',
    }),
  )
  await page.getByRole('button', { name: 'Download backup' }).click()
  await expect(page.locator('.backup-error')).toHaveText('Creating the backup failed: No space left on device.')
  await expect(page.getByRole('button', { name: 'Download backup' })).toBeEnabled()
})

test('does not offer Google Drive until it is connected', async ({ page }) => {
  // This environment has a Drive client but no account behind it.
  const drive = page.getByRole('button', { name: 'Back up to Google Drive' })
  await expect(drive).toBeDisabled()
  await expect(page.locator('.drive-hint')).toContainText('Connect Google Drive on the Storage tab')
})

test('saves a backup into Google Drive when it is connected', async ({ page }) => {
  // Connecting needs a real Google account, so the page is told Drive is ready
  // and given a Drive that accepts the upload: the API layer is mocked, not the
  // page (the same approach mocked-integrations.spec.ts takes for the Storage tab).
  await page.route('**/api/backup/preview', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    await route.fulfill({ response, json: { ...body, gdrive: { state: 'ready', folder: 'Blink Clips/Backups' } } })
  })
  await page.route('**/api/backup/gdrive', (route) =>
    route.fulfill({
      json: {
        name: 'blink-clips-backup-20261018-120000.zip',
        size_bytes: 2_097_152,
        folder: 'Blink Clips/Backups',
        file_id: 'f1',
      },
    }),
  )
  // The sidebar's Refresh reads what a backup holds again, picking up the mock.
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.locator('.drive-hint')).toHaveText('Saves into Blink Clips/Backups on your Google Drive.')

  await page.getByRole('button', { name: 'Back up to Google Drive' }).click()
  await expect(page.locator('.backup-done')).toHaveText(
    'Saved to Google Drive: Blink Clips/Backups/blink-clips-backup-20261018-120000.zip (2.0 MB).',
  )
})
