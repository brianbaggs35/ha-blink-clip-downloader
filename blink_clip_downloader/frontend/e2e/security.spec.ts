import { test, expect } from './coverage-fixtures'

// The Security tab reads from the security_events table, seeded directly by
// standalone_server.py against existing distribution clips (see _seed) —
// producing them for real would need a running YOLO, which this environment
// deliberately doesn't have. Everything below the seed is the real code
// path: real API handlers, real SQL (including the DISTINCT ON collapse to
// one row per clip), real Vue components.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="security"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="security"]')
})

test('shows one row per clip, collapsed to its most severe event', async ({ page }) => {
  const timeline = page.locator('[data-testid="security-timeline"]')
  await expect(timeline).toBeVisible()
  // Three seeded clips, newest first. e2e-clip-001 has two events and must
  // appear once, represented by the more severe of them.
  await expect(timeline.locator('.security-row')).toHaveCount(3)
  await expect(timeline.getByText('Impact candidate')).toBeVisible()
  await expect(timeline.getByText('Loitering')).toBeVisible()
  await expect(timeline.getByText('Subject present')).toHaveCount(1)
})

test('summarizes the recent window by severity', async ({ page }) => {
  const stats = page.locator('[data-testid="security-stats"]')
  await expect(stats).toContainText('Critical')
  await expect(stats).toContainText('Routine')
  await expect(stats).toContainText('clip(s) with security events in the last 7 days')
})

test('filters by camera', async ({ page }) => {
  await page.locator('.security-filter').first().click()
  await page.getByRole('option', { name: 'Backyard', exact: true }).click()
  const timeline = page.locator('[data-testid="security-timeline"]')
  await expect(timeline.locator('.security-row')).toHaveCount(1)
  await expect(timeline.getByText('Loitering')).toBeVisible()
})

test('filters by minimum severity', async ({ page }) => {
  await page.locator('.security-filter').nth(1).click()
  await page.getByRole('option', { name: 'Critical only' }).click()
  const timeline = page.locator('[data-testid="security-timeline"]')
  await expect(timeline.locator('.security-row')).toHaveCount(1)
  await expect(timeline.getByText('Impact candidate')).toBeVisible()
})

test("shows a clip's full evidence on demand", async ({ page }) => {
  const row = page.locator('.security-row', { hasText: 'Loitering' })
  await row.getByRole('button', { name: 'Show evidence' }).click()
  const detail = row.locator('[data-testid="security-detail"]')
  await expect(detail).toBeVisible()
  // Both of this clip's events, not just the one the timeline row showed.
  await expect(detail.locator('li')).toHaveCount(2)
  await expect(detail).toContainText('Risk')
  await expect(detail).toContainText('Evidence')
  await expect(detail).toContainText('may disagree')

  await row.getByRole('button', { name: 'Hide evidence' }).click()
  await expect(detail).toHaveCount(0)
})

test('opens the clip without leaving the tab', async ({ page }) => {
  // The whole point of the link: a timeline entry that names a clip and
  // then leaves you to go and find it is not much of a timeline.
  await page
    .locator('.security-row', { hasText: 'Impact candidate' })
    .getByRole('button', { name: 'View clip' })
    .click()
  await expect(page.locator('.modal-bg.open')).toBeVisible()
})

test('every row carries a thumbnail that opens its clip', async ({ page }) => {
  const row = page.locator('.security-row', { hasText: 'Impact candidate' })
  const thumb = row.locator('.security-thumb img')
  await expect(thumb).toBeVisible()
  await row.locator('.security-thumb').click()
  await expect(page.locator('.modal-bg.open')).toBeVisible()
})

test("an event's own timestamp opens the clip at that moment", async ({ page }) => {
  // Scrubbing by hand for "possible contact at 0:06" is the difference
  // between a log and something a person can actually review.
  const row = page.locator('.security-row', { hasText: 'Loitering' })
  await row.getByRole('button', { name: 'Show evidence' }).click()
  const detail = row.locator('[data-testid="security-detail"]')
  await expect(detail).toBeVisible()
  await detail.locator('.security-detail-time').first().click()
  await expect(page.locator('.modal-bg.open')).toBeVisible()
})

test('the empty state explains itself rather than showing a blank tab', async ({ page }) => {
  await page.route('**/api/security/timeline*', (route) => route.fulfill({ json: { events: [], total: 0 } }))
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="security"]').click()
  await expect(page.getByText('No security events yet')).toBeVisible()
})
