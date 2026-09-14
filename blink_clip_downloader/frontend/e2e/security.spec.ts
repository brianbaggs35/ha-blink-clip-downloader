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

test('a row whose thumbnail will not load shows no broken image', async ({ page }) => {
  // The seeded clips have no thumbnail file on disk, which is exactly the
  // case worth proving here: the row drops the image rather than rendering
  // a broken one, and everything else about it still works.
  const row = page.locator('.security-row', { hasText: 'Impact candidate' })
  await expect(row).toBeVisible()
  await expect(row.locator('.security-thumb')).toHaveCount(0)
  await expect(row.getByRole('button', { name: 'View clip' })).toBeVisible()
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

test('the severity filter includes everything at or above the chosen band', async ({ page }) => {
  // "Critical only" is covered above; this is the other half of
  // _severities_at_or_above — a band that must sweep up everything more
  // severe than itself, not just its own name. The routine-only clip drops
  // out; the suspicious and critical ones stay.
  await page.locator('.security-filter').nth(1).click()
  await page.getByRole('option', { name: 'Noteworthy and up' }).click()
  const timeline = page.locator('[data-testid="security-timeline"]')
  await expect(timeline.locator('.security-row')).toHaveCount(2)
  await expect(timeline.getByText('Impact candidate')).toBeVisible()
  await expect(timeline.getByText('Loitering')).toBeVisible()
  await expect(timeline.getByText('Subject present')).toHaveCount(0)
})

test('the period filter narrows to the chosen window', async ({ page }) => {
  // Filtered on when each clip was *recorded*, not when it was analyzed —
  // the timeline is ordered by clip time, so a backlog processed overnight
  // must not file three-day-old footage under this week. All three seeded
  // clips are hours old, so both windows keep all three; the request
  // assertion is what proves the choice actually reaches the query rather
  // than being dropped on the way (deliberately not "Today", whose bound is
  // local midnight — an 11-hour-old clip falls on either side of it
  // depending on the hour the suite happens to run).
  await page.locator('.p-selectbutton').getByText('Week', { exact: true }).click()
  const timeline = page.locator('[data-testid="security-timeline"]')
  await expect(timeline.locator('.security-row')).toHaveCount(3)

  const requested: string[] = []
  page.on('request', (request) => {
    if (request.url().includes('/api/security/timeline')) requested.push(request.url())
  })
  await page.locator('.p-selectbutton').getByText('Month', { exact: true }).click()
  await expect(timeline.locator('.security-row')).toHaveCount(3)
  expect(requested.some((url) => url.includes('period=month'))).toBe(true)
})

test('stepping to the next clip from a Security-opened modal stays where it was', async ({ page }) => {
  // A clip opened from here is generally not in the Library's own filtered
  // list, which used to make prev/next find index -1, step to 0 and
  // teleport the modal to the newest clip in the library. The modal should
  // simply stay on the clip it was showing.
  await page
    .locator('.security-row', { hasText: 'Impact candidate' })
    .getByRole('button', { name: 'View clip' })
    .click()
  const modal = page.locator('.modal-bg.open')
  await expect(modal).toBeVisible()
  const title = await modal.locator('.modal-title').textContent()

  await page.keyboard.press('ArrowDown')
  await expect(modal.locator('.modal-title')).toHaveText(title ?? '')
  await page.keyboard.press('ArrowUp')
  await expect(modal.locator('.modal-title')).toHaveText(title ?? '')
  await expect(modal).toBeVisible()
})
