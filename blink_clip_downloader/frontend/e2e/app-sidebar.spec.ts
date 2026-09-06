import { test, expect } from './coverage-fixtures'

// AppSidebar is global chrome — always mounted regardless of the active
// tab — so unlike every other spec file here, this isn't scoped to one tab.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('theme toggle switches the dark/light aria-label back and forth', async ({ page }) => {
  // Defaults to dark (theme.ts: unset localStorage means dark, not a
  // prefers-color-scheme lookup), so the toggle starts offering light.
  await expect(page.getByRole('button', { name: 'Switch to light theme' })).toBeVisible()

  await page.getByRole('button', { name: 'Switch to light theme' }).click()
  await expect(page.getByRole('button', { name: 'Switch to dark theme' })).toBeVisible()

  await page.getByRole('button', { name: 'Switch to dark theme' }).click()
  await expect(page.getByRole('button', { name: 'Switch to light theme' })).toBeVisible()
})

test('the help button opens the keyboard shortcuts overlay', async ({ page }) => {
  await page.getByRole('button', { name: 'Keyboard shortcuts' }).click()
  const overlay = page.locator('.modal-bg.open')
  await expect(overlay.locator('.modal-title')).toContainText('Keyboard Shortcuts')

  await overlay.locator('.modal-close').click()
  await expect(overlay).toHaveCount(0)
})

test('the About dialog shows the repo links', async ({ page }) => {
  await page.getByRole('button', { name: 'About this app' }).click()
  const dialog = page.getByRole('dialog', { name: 'About Blink Clips 5.4.8' })
  await expect(dialog).toContainText('Built by Brian Baggs.')
  await expect(dialog.getByRole('link', { name: /ha-blink-clip-downloader/ })).toBeVisible()
})

test('Refresh bumps the cross-tab refresh signal, and Sync triggers a real download-now call', async ({ page }) => {
  // No directly visible effect of its own (refresh.bump() just notifies
  // other components' watchers) — still exercises onRefreshClick for real,
  // and the Sync click below proves the page stayed fully interactive.
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()

  await page.getByRole('button', { name: 'Sync', exact: true }).click()
  await expect(page.getByText('Download triggered — clips appear shortly')).toBeVisible()
})
