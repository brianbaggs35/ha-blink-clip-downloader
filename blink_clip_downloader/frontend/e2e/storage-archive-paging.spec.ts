import { test, expect } from './coverage-fixtures'

// Archiving is a long-lived, accumulating feature: someone a year in has
// far more archives than a throwaway test database ever seeds, and far
// more clips inside one of them. Both paginators, and a single archive
// that fails to expand while its neighbours work, are therefore states
// only a served payload can reach.

function group(i: number) {
  const day = String((i % 28) + 1).padStart(2, '0')
  return {
    archive_path: `/share/archives/2025-06-${day}-batch-${i}.zip`,
    clip_count: i === 0 ? 120 : 3,
    total_size: 4_000_000 + i * 1000,
    latest_timestamp: `2025-06-${day}T0${i % 10}:00:00Z`,
  }
}

function clip(i: number, archive: string) {
  return {
    id: `arch-${i}`,
    camera: i % 2 ? 'Front Door' : 'Backyard',
    file_path: `${archive}!/clip-${i}.mp4`,
    timestamp: '2025-06-01T09:00:00Z',
    size: 1000,
    duration: 10,
    source: 'archive',
    starred: false,
    tags: [],
    analyzed: false,
    suspicious: false,
    notified: false,
    face_recognized: false,
  }
}

const GROUPS = Array.from({ length: 14 }, (_, i) => group(i))
const BIG = GROUPS[0].archive_path

async function serveArchives(page: import('@playwright/test').Page, opts: { failArchive?: string } = {}) {
  await page.route('**/api/storage/archives**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(GROUPS) }),
  )
  await page.route('**/api/storage/archive-clips**', (route) => {
    const url = new URL(route.request().url())
    const archive = url.searchParams.get('archive_path') ?? ''
    if (opts.failArchive && archive === opts.failArchive) {
      return route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
    }
    const total = archive === BIG ? 120 : 3
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? 50)
    const items = Array.from({ length: Math.min(limit, total - offset) }, (_, k) => clip(offset + k, archive))
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items, total }),
    })
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('more archives than fit on a page are paginated rather than all rendered', async ({ page }) => {
  await serveArchives(page)
  // Ten per page, fourteen in total.
  await expect(page.locator('.archive-panel')).toHaveCount(10)
  await page.locator('#page-storage').getByRole('button', { name: 'Next Page' }).first().click()
  await expect(page.locator('.archive-panel')).toHaveCount(4)
})

test('an archive with more clips than one page shows its own paginator', async ({ page }) => {
  await serveArchives(page)
  const panel = page.locator('.archive-panel').first()
  await panel.locator('.archive-panel-header').click()

  // 120 clips, 50 to a page: the table shows one page, and the archive's
  // own paginator appears to reach the rest.
  await expect(panel.locator('.archive-clips-table-wrap tbody tr[data-p-index]')).toHaveCount(50)
  await expect(panel.locator('.archive-clips-paginator')).toBeVisible()
})

test('one archive failing to expand says so without breaking its neighbours', async ({ page }) => {
  await serveArchives(page, { failArchive: BIG })
  const panels = page.locator('.archive-panel')
  await panels.first().locator('.archive-panel-header').click()
  await expect(panels.first()).toContainText('Failed to load clips in this archive.')

  // The next one along still opens normally -- the error is per-archive,
  // not a dead tab.
  await panels.nth(1).locator('.archive-panel-header').click()
  await expect(panels.nth(1).locator('.archive-clips-table-wrap tbody tr[data-p-index]')).toHaveCount(3)
})
