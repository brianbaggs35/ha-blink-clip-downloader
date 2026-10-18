import type { Page } from '@playwright/test'
import { test, expect } from './coverage-fixtures'
import { scratchClipBackup } from './backup-fixtures'

// What the page does with a file it should not trust. None of these ends in a
// restore, so none of them touches the library.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="backup"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="backup"]')
})

async function choose(page: Page, name: string, buffer: Buffer) {
  await page
    .locator('#page-backup input[type="file"]')
    .setInputFiles({ name, mimeType: 'application/octet-stream', buffer })
}

async function expectRefused(page: Page, reason: string | RegExp) {
  await expect(page.locator('.restore-error')).toContainText(reason)
  // Back at the start, ready for another file, with nothing to restore.
  await expect(page.locator('.restore-choose')).toBeVisible()
  await expect(page.locator('.restore-summary')).toHaveCount(0)
}

test('a file that is not a zip is turned away before it is sent', async ({ page }) => {
  let uploads = 0
  await page.route('**/api/backup/uploads', (route) => {
    uploads++
    return route.continue()
  })
  await choose(page, 'photo.jpg', Buffer.from('not a zip'))
  await expectRefused(page, 'not a .zip file')
  expect(uploads).toBe(0)
})

test('a file named like a zip that is not one is refused by the add-on', async ({ page }) => {
  await choose(page, 'backup.zip', Buffer.from('definitely not a zip archive'))
  await expectRefused(page, 'not a readable backup')
})

test('a zip that is not a backup is refused', async ({ page }) => {
  const { zip } = await import('./backup-fixtures')
  await choose(page, 'other.zip', zip([{ name: 'readme.txt', data: Buffer.from('hello') }]))
  await expectRefused(page, 'no manifest.json')
})

test('a backup edited after it was made is refused as damaged', async ({ page }) => {
  await choose(page, 'edited.zip', scratchClipBackup({ tamper: true }))
  await expectRefused(page, /damaged/)
})

test('a backup from a newer version says to update the add-on', async ({ page }) => {
  await choose(page, 'future.zip', scratchClipBackup({ formatVersion: 2 }))
  await expectRefused(page, 'made by a newer version of the add-on')
})

test('a refused file can be followed by a good one', async ({ page }) => {
  await choose(page, 'broken.zip', Buffer.from('nope'))
  await expectRefused(page, 'not a readable backup')

  await choose(page, 'good.zip', scratchClipBackup())
  await expect(page.locator('.restore-summary')).toBeVisible()
  // The earlier complaint is gone with the new attempt.
  await expect(page.locator('.restore-error')).toHaveCount(0)
})
