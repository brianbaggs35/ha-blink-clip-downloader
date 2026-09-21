import { test, expect } from './coverage-fixtures'

// Two states in the Archived Clips section that nothing reached. Both use
// the real seeded archives rather than served ones — the seed already has
// a single-clip archive, and the camera list is real — so these exercise
// the same code path a user would, and neither deletes anything.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
})

test('deleting an archive of one clip asks about "1 clip", not "1 clips"', async ({ page }) => {
  // storage.spec.ts declines this confirmation on a two-clip archive,
  // which only ever produces the plural. 2024-02-e2e.zip is the seeded
  // single-clip one. Declined, so nothing is removed.
  const single = page.locator('.archive-panel', { hasText: '2024-02-e2e.zip' })
  await expect(single).toContainText('1 clip')

  await single.getByRole('button', { name: 'Delete archive' }).click()
  await expect(page.getByText('Delete archive?')).toBeVisible()
  await expect(page.getByText(/permanently removes all 1 clip in/)).toBeVisible()

  await page.getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByText('Delete archive?')).toHaveCount(0)
  // Still there, and still counted the same.
  await expect(page.locator('.archive-panel')).toHaveCount(3)
})

test('a camera filter left pointing at a camera that has gone resets itself', async ({ page }) => {
  await page.getByRole('combobox', { name: 'Filter by camera' }).click()
  await page.getByRole('option', { name: 'Front Door' }).click()
  await expect(page.locator('.archive-panel')).toHaveCount(1)

  // The camera list comes back without it — what a rename in the Blink app
  // looks like from here. Holding the filter would show an empty archive
  // list with nothing explaining why.
  await page.route('**/api/cameras', (route) =>
    // A bare array: /api/cameras returns CameraStat[], not an envelope.
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()

  // The filter itself resets as soon as the camera list comes back without
  // it. (The archive list is re-fetched concurrently, so it can still be
  // showing the narrowed result at this moment — a tab revisit settles it,
  // which is what the remount below checks.)
  await expect(page.getByRole('combobox', { name: 'Filter by camera' })).toContainText('All')

  // Leaving and returning remounts the section, which reloads both with the
  // filter already reset. A tab switch rather than a reload: a navigation
  // would discard this test's coverage before the fixture collects it.
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
  await expect.poll(() => page.locator('.archive-panel').count()).toBe(3)
})
