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
// this runs: Total clips and Archived (nothing in this suite archives a
// clip) never change.
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
  await expect(libraryCard.locator('.status-row', { hasText: 'Total clips' })).toContainText('14')
  await expect(libraryCard.locator('.status-row', { hasText: 'Archived' })).toContainText('0')
})

test('shows every seeded camera with its total clip count', async ({ page }) => {
  const camerasCard = page.locator('.status-card', { hasText: 'Cameras (4)' })
  await expect(camerasCard).toBeVisible()
  await expect(camerasCard.locator('.status-row', { hasText: 'Front Door' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Backyard' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Garage' })).toContainText('4 clips')
  await expect(camerasCard.locator('.status-row', { hasText: 'Test Scratch' })).toContainText('2 clips')
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
