import type { Page } from '@playwright/test'
import { test, expect } from './coverage-fixtures'

// Exercises the clip modal against real, seeded data. Mutating tests
// (star/tag) use the two "Test Scratch" camera clips seeded by
// scripts/standalone_server.py specifically for this — never the
// "distribution" clips library-filters.spec.ts's counts depend on.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

// HelpOverlay/PromptOverlay/TwoFAOverlay/ConfirmDialog all share the same
// .modal-bg/.modal-title/.modal-close class names as ClipModal (see
// ../src/App.vue) — an unscoped `page.locator('.modal-close')` etc.
// matches all of them at once and throws Playwright's strict-mode
// violation, even though only ClipModal is ever actually open here.
function openModal(page: Page) {
  return page.locator('.modal-bg.open')
}

test('opening a clip shows its real seeded metadata', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  await expect(modal.locator('.modal-title')).toContainText('Front Door')
  const metaGrid = modal.locator('.meta-grid')
  await expect(metaGrid).toContainText('pir')
  await expect(metaGrid).toContainText('8s')
})

test('starring a clip from the modal updates the grid immediately and survives a reload', async ({ page }) => {
  const card = page.locator('.clip-card[data-id="e2e-scratch-star"]')
  await expect(card.locator('.star-badge')).toHaveCount(0)

  await card.click()
  const modal = openModal(page)
  const starBtn = modal.getByRole('button', { name: /Star/ })
  await expect(starBtn).toHaveText('☆ Star')
  await starBtn.click()
  await expect(starBtn).toHaveText('★ Starred')

  await modal.locator('.modal-close').click()
  // LibraryPage.vue's onStarred() patches the in-memory clip in place, so
  // this is expected to appear without a reload — unlike tags, see below.
  await expect(card.locator('.star-badge')).toBeVisible()

  await page.reload()
  await expect(page.locator('.clip-card[data-id="e2e-scratch-star"] .star-badge')).toBeVisible()
})

test('adding a tag from the modal persists and is visible on the card after reload', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-scratch-tag"]').click()
  const modal = openModal(page)
  await modal.locator('#clip-tag-input').fill('e2e-added-tag')
  await modal.locator('#clip-tag-input').press('Enter')
  await expect(modal.locator('.tag-item', { hasText: 'e2e-added-tag' })).toBeVisible()

  await modal.locator('.modal-close').click()
  // Unlike starring, ClipModal doesn't emit a tags-updated event, so the
  // grid's in-memory clip list isn't patched in place — only a reload
  // (a real refetch) picks up the new tag. Documents actual behavior, not
  // an assumption.
  await page.reload()
  await expect(page.locator('.clip-card[data-id="e2e-scratch-tag"] .tag-pill')).toHaveText('e2e-added-tag')
})

test('Escape closes the modal', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  await expect(openModal(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(openModal(page)).toHaveCount(0)
})

test('the close button closes the modal', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-001"]').click()
  const modal = openModal(page)
  await expect(modal).toBeVisible()
  await modal.locator('.modal-close').click()
  await expect(openModal(page)).toHaveCount(0)
})

test('theater mode toggles a class on the modal and its own label', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  // Located by its stable title attribute, not its name/text — the button's
  // own accessible name flips between "Theater"/"Normal" on every click, so
  // a locator bound to one of those text values stops matching as soon as
  // it changes.
  const theaterBtn = modal.locator('button[title="Theater mode"]')
  await expect(modal.locator('.modal')).not.toHaveClass(/theater/)
  await expect(theaterBtn).toHaveText(/Theater/)

  await theaterBtn.click()
  await expect(modal.locator('.modal')).toHaveClass(/theater/)
  await expect(theaterBtn).toHaveText(/Normal/)

  await theaterBtn.click()
  await expect(modal.locator('.modal')).not.toHaveClass(/theater/)
  await expect(theaterBtn).toHaveText(/Theater/)
})

test('auto-play and loop checkboxes toggle independently', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  const autoplay = modal.getByLabel('Auto-play next clip')
  const loop = modal.getByLabel('Loop')
  await expect(autoplay).not.toBeChecked()
  await expect(loop).not.toBeChecked()

  await autoplay.check()
  await expect(autoplay).toBeChecked()
  await expect(loop).not.toBeChecked()

  await loop.check()
  await expect(loop).toBeChecked()
  await expect(autoplay).toBeChecked()
})

test('prev/next nav buttons and arrow keys move between clips in the same sort order as the grid', async ({ page }) => {
  // e2e-clip-000 (0h ago) and e2e-clip-001 (1h ago) are adjacent under the
  // default "newest first" sort — real, seeded ordering, not a mock.
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  await expect(modal.locator('.modal-title')).toContainText('Front Door')

  await modal.locator('.vid-nav-btn[title^="Next"]').click()
  await expect(modal.locator('.modal-title')).toContainText('Backyard')

  await modal.locator('.vid-nav-btn[title^="Previous"]').click()
  await expect(modal.locator('.modal-title')).toContainText('Front Door')

  await page.keyboard.press('ArrowDown')
  await expect(modal.locator('.modal-title')).toContainText('Backyard')
  await page.keyboard.press('ArrowUp')
  await expect(modal.locator('.modal-title')).toContainText('Front Door')
})

test('the download link and video-unavailable fallback point at the real clip stream', async ({ page }) => {
  // None of the seeded distribution clips have a real file on disk (only a
  // dedicated archiving-test clip does — see standalone_server.py), so
  // Video.js reliably hits its error path here, exercising the fallback UI
  // instead of just the happy path — a real, not-uncommon-in-production
  // case per ClipModal.vue's own videoError comment, not an artificial
  // test-only scenario. Not asserting toBeVisible() on .video-fallback
  // itself: its layout height comes from its thumbnail <img>'s own natural
  // size, and that thumbnail 404s for the same "no real file on disk"
  // reason, collapsing it to zero height here — a fixture-data limitation
  // (same one VehicleZonePicker's e2e coverage is blocked by), not a bug.
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  await expect(modal.locator('.video-fallback')).toContainText("isn't available")

  // Exact match: the fallback panel's own "Download instead" link also
  // matches a loose /Download/ pattern.
  const downloadLink = modal.getByRole('link', { name: '⬇ Download', exact: true })
  await expect(downloadLink).toHaveAttribute('href', '/api/clips/e2e-clip-000/stream')
})

test('copying the file path shows a confirmation toast', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  await modal.getByRole('button', { name: 'Path' }).click()
  await expect(page.getByText('File path copied')).toBeVisible()
})

test('expanding the AI panel and clicking Analyze Now shows a real analysis result', async ({ page }) => {
  // The real (but unreachable-provider) ClipAnalyzer wired into
  // standalone_server.py still runs its own local frame-extraction step
  // before ever reaching the network — since this clip has no real file on
  // disk, that step genuinely fails, producing a deterministic result from
  // the actual analyze_clip code path rather than a mocked stand-in.
  // e2e-clip-001, not -000: ai.spec.ts's "Test Analysis" test always
  // analyzes the single most-recent clip (e2e-clip-000, 0h old) — using a
  // different clip here avoids depending on cross-file run order for which
  // one is "not analyzed yet" when this test starts.
  await page.locator('.clip-card[data-id="e2e-clip-001"]').click()
  const modal = openModal(page)
  await modal.locator('.ai-panel-hdr').click()
  await expect(modal.getByText('Not analyzed yet')).toBeVisible()

  await modal.getByRole('button', { name: 'Analyze Now' }).click()
  await expect(modal.getByText('No frames could be extracted')).toBeVisible()
  await expect(modal.locator('.ai-badge-clean')).toHaveText('✓ Clear')
})
