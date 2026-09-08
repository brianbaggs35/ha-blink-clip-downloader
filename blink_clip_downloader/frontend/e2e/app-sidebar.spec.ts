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
  const dialog = page.getByRole('dialog', { name: 'About Blink Clips 5.5.0' })
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

// The three tests below exercise useKeyboardShortcuts.ts's global `?`/Esc
// handling directly via the keyboard, rather than the help button's own
// @click -- a separate code path (see the button test above, which never
// touches onKeydown at all).

test('the ? key opens the keyboard shortcuts overlay', async ({ page }) => {
  await page.keyboard.press('?')
  const overlay = page.locator('.modal-bg.open')
  await expect(overlay.locator('.modal-title')).toContainText('Keyboard Shortcuts')
})

test('the Escape key closes the keyboard shortcuts overlay', async ({ page }) => {
  await page.getByRole('button', { name: 'Keyboard shortcuts' }).click()
  const overlay = page.locator('.modal-bg.open')
  await expect(overlay.locator('.modal-title')).toContainText('Keyboard Shortcuts')

  await page.keyboard.press('Escape')
  await expect(page.locator('.modal-bg.open')).toHaveCount(0)
})

test('typing ? into a text field does not open the keyboard shortcuts overlay', async ({ page }) => {
  // onKeydown excludes INPUT/TEXTAREA/SELECT/contenteditable specifically so
  // a real '?' keystroke while filtering the Library search box (or the
  // Vehicles description, or any other text field) doesn't hijack it.
  // locator.press (unlike .fill, which sets the value directly with no real
  // keydown event) dispatches a genuine keydown on the focused element, so
  // this actually exercises the tagName guard rather than trivially passing.
  const search = page.locator('#search')
  await search.press('?')
  await expect(search).toHaveValue('?')
  await expect(page.locator('.modal-bg.open')).toHaveCount(0)
})
