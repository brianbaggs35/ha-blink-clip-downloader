import { test, expect } from '@playwright/test'

// Exercises the Library tab's filters against real, seeded data from a
// real backend (scripts/standalone_server.py) — not mocked fetch, unlike
// LibraryPage.spec.ts's Vitest tests. Read-only: every assertion here
// targets the 12 "distribution" clips + 2 "Test Scratch" clips seeded by
// that script, deliberately never mutated by any test (see
// library-modal.spec.ts, which uses its own separate scratch clips) — so
// these counts stay correct no matter what order specs run in.
const TOTAL_CLIPS = 14
const CLIPS_PER_CAMERA = 4

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('loads with the seeded clips and per-camera counts in the sidebar', async ({ page }) => {
  await expect(page.locator('.app-nav-cam[data-camera="all"] .app-nav-cam-count')).toHaveText(String(TOTAL_CLIPS))
  for (const camera of ['Front Door', 'Backyard', 'Garage']) {
    await expect(page.locator(`.app-nav-cam[data-camera="${camera}"] .app-nav-cam-count`)).toHaveText(
      String(CLIPS_PER_CAMERA),
    )
  }
  await expect(page.locator('.clip-card')).toHaveCount(TOTAL_CLIPS)
})

test('clicking a camera filters the grid to just that camera, and "All Cameras" restores the rest', async ({
  page,
}) => {
  await page.locator('.app-nav-cam[data-camera="Garage"]').click()
  await expect(page.locator('.clip-card')).toHaveCount(CLIPS_PER_CAMERA)
  const cameras = await page.locator('.clip-card .clip-camera').allTextContents()
  expect(cameras.every((c) => c === 'Garage')).toBe(true)

  await page.locator('.app-nav-cam[data-camera="all"]').click()
  await expect(page.locator('.clip-card')).toHaveCount(TOTAL_CLIPS)
})

test('source filter shows only clips recorded from that source', async ({ page }) => {
  await page.locator('#source-filter').click()
  await page.getByRole('option', { name: 'Motion (PIR)' }).click()
  // 4 distribution clips seeded as pir + the 2 Test Scratch clips (also pir).
  await expect(page.locator('.clip-card')).toHaveCount(CLIPS_PER_CAMERA + 2)
})

test('starred filter shows only the pre-starred clips', async ({ page }) => {
  await page.locator('#lib-filter-starred').check()
  await expect(page.locator('.clip-card')).toHaveCount(3)
  await expect(page.locator('.clip-card .star-badge')).toHaveCount(3)
})

test('tag filter shows only clips carrying that tag', async ({ page }) => {
  await page.locator('#tag-filter').click()
  await page.getByRole('option', { name: 'delivery' }).click()
  await expect(page.locator('.clip-card')).toHaveCount(2)
  for (const pill of await page.locator('.clip-card .tag-pill').allTextContents()) {
    expect(pill).toBe('delivery')
  }
})

test('search filters by camera name', async ({ page }) => {
  await page.locator('#search').fill('Garage')
  await expect(page.locator('.clip-card')).toHaveCount(CLIPS_PER_CAMERA)
})

test('search for a nonexistent camera shows the empty state', async ({ page }) => {
  await page.locator('#search').fill('no-such-camera-exists')
  await expect(page.getByText('No clips found')).toBeVisible()
  await expect(page.locator('.clip-card')).toHaveCount(0)
})
