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

// The four tests below all reuse e2e-scratch-tag like the "adding a tag"
// test above, but each adds/removes its own uniquely-named tag rather than
// touching "e2e-added-tag" -- self-contained regardless of what order these
// tests (or the one above) happen to run in.

test('adding the same tag twice does not duplicate it', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-scratch-tag"]').click()
  const modal = openModal(page)
  const input = modal.locator('#clip-tag-input')

  await input.fill('e2e-dup-test')
  await input.press('Enter')
  await input.fill('e2e-dup-test')
  await input.press('Enter')

  await expect(modal.locator('.tag-item', { hasText: 'e2e-dup-test' })).toHaveCount(1)
})

test('tag text is normalized to lowercase, hyphenated, alphanumeric', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-scratch-tag"]').click()
  const modal = openModal(page)
  const input = modal.locator('#clip-tag-input')

  await input.fill('E2E Normalize Test!')
  await input.press('Enter')

  await expect(modal.locator('.tag-item', { hasText: 'e2e-normalize-test' })).toBeVisible()
  await expect(modal.locator('.tag-item', { hasText: 'E2E Normalize Test!' })).toHaveCount(0)
})

test('removing a tag asks for confirmation, and declining leaves it', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-scratch-tag"]').click()
  const modal = openModal(page)
  const input = modal.locator('#clip-tag-input')
  await input.fill('e2e-remove-test')
  await input.press('Enter')
  const tag = modal.locator('.tag-item', { hasText: 'e2e-remove-test' })
  await expect(tag).toBeVisible()

  await tag.locator('.rm').click()
  await expect(page.getByText('Remove tag "e2e-remove-test" from this clip?')).toBeVisible()
  await page.getByRole('button', { name: 'Cancel' }).click()
  await expect(tag).toBeVisible()

  await tag.locator('.rm').click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(modal.locator('.tag-item', { hasText: 'e2e-remove-test' })).toHaveCount(0)
})

test('Escape blurs the tag input instead of closing the modal', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-scratch-tag"]').click()
  const modal = openModal(page)
  const input = modal.locator('#clip-tag-input')
  await input.click()
  await expect(input).toBeFocused()

  await page.keyboard.press('Escape')
  await expect(input).not.toBeFocused()
  await expect(modal).toBeVisible()
})

test('playback shortcut keys are suppressed while the tag input is focused', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-003"]').click()
  const modal = openModal(page)
  const input = modal.locator('#clip-tag-input')
  await input.click()
  await expect(input).toBeFocused()

  // 'l' would normally toggle Loop and show a toast -- typing it into the
  // tag input must do neither (onKeydown's isTextInput guard).
  await page.keyboard.type('l')
  await expect(page.getByText('Loop ON')).toHaveCount(0)
  await expect(input).toHaveValue('l')
  await input.fill('')
})

test('playback keyboard shortcuts control the player: play/pause, seek, mute, and fullscreen', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-003"]').click()
  const modal = openModal(page)
  await expect(modal).toBeVisible()

  // None of these should close the modal or throw -- the player survives
  // the whole sequence either way, whether or not the (fileless) clip ever
  // actually loads enough to seek/mute for real.
  await page.keyboard.press(' ')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowLeft')
  await page.keyboard.press('m')
  await expect(modal).toBeVisible()

  // Whether requestFullscreen() actually succeeds or rejects in this
  // sandboxed/headless browser isn't something to assert on either way --
  // just that the F handler runs and the modal survives it.
  await page.keyboard.press('f')
  await expect(modal).toBeVisible()
})

test('declining the delete confirmation keeps the clip', async ({ page }) => {
  await page.locator('.clip-card[data-id="e2e-clip-003"]').click()
  const modal = openModal(page)
  await modal.getByRole('button', { name: '🗑 Delete' }).click()
  await expect(page.getByText('Delete this clip permanently?')).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByText('Delete this clip permanently?')).toHaveCount(0)
  await expect(modal).toBeVisible()

  await modal.locator('.modal-close').click()
  await expect(page.locator('.clip-card[data-id="e2e-clip-003"]')).toBeVisible()
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

test('the L keyboard shortcut toggles loop with its own confirmation toast', async ({ page }) => {
  // A genuinely different code path from the Loop checkbox above —
  // onKeydown's 'l' case is the only place that shows the "Loop ON"/
  // "Loop OFF" toast; ticking the checkbox directly doesn't.
  await page.locator('.clip-card[data-id="e2e-clip-000"]').click()
  const modal = openModal(page)
  const loop = modal.getByLabel('Loop')
  await expect(loop).not.toBeChecked()

  await page.keyboard.press('l')
  await expect(page.getByText('Loop ON')).toBeVisible()
  await expect(loop).toBeChecked()

  await page.keyboard.press('l')
  await expect(page.getByText('Loop OFF')).toBeVisible()
  await expect(loop).not.toBeChecked()
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

  await modal.getByRole('button', { name: 'Full response' }).click()
  await expect(modal.getByRole('button', { name: 'Hide response' })).toBeVisible()

  await modal.getByRole('button', { name: '↺ Re-analyze' }).click()
  await expect(page.getByText('AI analysis complete')).toBeVisible()
  await expect(modal.locator('.ai-badge-clean')).toHaveText('✓ Clear')

  await modal.getByRole('button', { name: '👍 Correct' }).click()
  await expect(page.getByText('Feedback recorded — thanks!')).toBeVisible()
  await expect(modal.getByText('👍 Marked correct')).toBeVisible()

  await modal.getByRole('button', { name: 'Clear', exact: true }).click()
  await expect(page.getByText('Feedback cleared')).toBeVisible()
  await expect(modal.getByText('Was this verdict correct?')).toBeVisible()

  await modal.getByRole('button', { name: '👎 Incorrect' }).click()
  await modal.locator('#clip-ai-feedback-note').fill('e2e: actually a delivery')
  await modal.locator('label', { hasText: 'flagged suspicious' }).locator('input[type="checkbox"]').check()
  await modal.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('Feedback recorded — thanks!').last()).toBeVisible()
  await expect(modal.getByText('👎 Marked incorrect')).toBeVisible()
  await expect(modal.getByText('"e2e: actually a delivery"')).toBeVisible()

  // biometrics.spec.ts (runs before this file alphabetically) leaves
  // exactly two enrolled people (Alex E2E, Jordan E2E — Casey E2E was
  // renamed then removed there), so more than one name on file means this
  // shows the picker rather than auto-submitting against a lone name.
  await modal.getByRole('button', { name: 'Report a missed face match' }).click()
  await expect(modal.locator('#clip-ai-face-report-name')).toBeVisible()
  await modal.locator('#clip-ai-face-report-name').selectOption('Alex E2E')
  await modal.getByRole('button', { name: 'Submit report' }).click()
  await expect(page.getByText('Thanks, reported — visible on the Biometrics activity card')).toBeVisible()
  await expect(modal.getByText('✓ Reported — thanks')).toBeVisible()
})

test('shows an error when the AI analysis result fails to load', async ({ page }) => {
  await page.route('**/api/ai/results/*', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  await page.locator('.clip-card[data-id="e2e-clip-002"]').click()
  const modal = openModal(page)
  await modal.locator('.ai-panel-hdr').click()
  await expect(modal.getByText('Failed to load analysis')).toBeVisible()
})

test('the feedback form can be cancelled, submitted without the correction checkbox, and changed afterward', async ({
  page,
}) => {
  await page.locator('.clip-card[data-id="e2e-clip-010"]').click()
  const modal = openModal(page)
  await modal.locator('.ai-panel-hdr').click()
  await expect(modal.getByText('Not analyzed yet')).toBeVisible()
  await modal.getByRole('button', { name: 'Analyze Now' }).click()
  await expect(page.getByText('AI analysis complete')).toBeVisible()

  await modal.getByRole('button', { name: '👎 Incorrect' }).click()
  await modal.getByRole('button', { name: 'Cancel' }).click()
  await expect(modal.locator('#clip-ai-feedback-note')).toHaveCount(0)
  await expect(modal.getByText('Was this verdict correct?')).toBeVisible()

  // Leaves the correction checkbox unchecked this time -- corrected_suspicious
  // stays undefined instead of flipping the clip's verdict.
  await modal.getByRole('button', { name: '👎 Incorrect' }).click()
  await modal.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('Feedback recorded — thanks!')).toBeVisible()
  await expect(modal.getByText('👎 Marked incorrect')).toBeVisible()

  await modal.getByRole('button', { name: 'Change' }).click()
  await expect(modal.getByText('Was this verdict correct?')).toBeVisible()
})

test('a single enrolled person auto-submits a missed-match report without showing the picker, and the picker can be cancelled or fail to submit', async ({
  page,
}) => {
  await page.locator('.clip-card[data-id="e2e-clip-011"]').click()
  const modal = openModal(page)
  await modal.locator('.ai-panel-hdr').click()
  await modal.getByRole('button', { name: 'Analyze Now' }).click()
  await expect(page.getByText('AI analysis complete')).toBeVisible()

  // biometrics.spec.ts leaves two enrolled people, so the real (unmocked)
  // picker shows first -- cancelling it should leave no report submitted.
  await modal.getByRole('button', { name: 'Report a missed face match' }).click()
  await expect(modal.locator('#clip-ai-face-report-name')).toBeVisible()
  await modal.getByRole('button', { name: 'Cancel' }).click()
  await expect(modal.locator('#clip-ai-face-report-name')).toHaveCount(0)

  await page.route('**/api/ai/faces/feedback/*', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  await modal.getByRole('button', { name: 'Report a missed face match' }).click()
  await modal.locator('#clip-ai-face-report-name').selectOption('Alex E2E')
  await modal.getByRole('button', { name: 'Submit report' }).click()
  await expect(page.getByText('Failed to save the report')).toBeVisible()
  await expect(modal.getByText('✓ Reported — thanks')).toHaveCount(0)

  // Now with only one enrolled person on file: startFaceReport() auto-
  // submits directly (report a *different* clip so faceReportSubmitted
  // from the failed attempt above doesn't already hide the report buttons).
  await page.unroute('**/api/ai/faces/feedback/*')
  await page.route('**/api/ai/faces', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ faces: [{ name: 'Solo E2E', approved: true }] }),
    }),
  )
  await modal.locator('.modal-close').click()
  await page.locator('.clip-card[data-id="e2e-clip-009"]').click()
  const modal2 = openModal(page)
  await modal2.locator('.ai-panel-hdr').click()
  await modal2.getByRole('button', { name: 'Analyze Now' }).click()
  await expect(page.getByText('AI analysis complete').last()).toBeVisible()

  await modal2.getByRole('button', { name: 'Report a missed face match' }).click()
  await expect(modal2.locator('#clip-ai-face-report-name')).toHaveCount(0)
  await expect(page.getByText('Thanks, reported — visible on the Biometrics activity card')).toBeVisible()
  await expect(modal2.getByText('✓ Reported — thanks')).toBeVisible()
})

test('shows an error toast when deleting a clip from the modal fails, and keeps the clip', async ({ page }) => {
  // The DELETE call is mocked to fail, so nothing is actually removed --
  // safe to use any seeded clip, including a "distribution" one whose
  // exact count other spec files depend on (library-filters.spec.ts).
  await page.route('**/api/clips/e2e-clip-002', (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback()
    return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) })
  })

  await page.locator('.clip-card[data-id="e2e-clip-002"]').click()
  const modal = openModal(page)
  await modal.getByRole('button', { name: '🗑 Delete' }).click()
  await expect(page.getByText('Delete this clip permanently?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Failed to delete clip')).toBeVisible()
  await expect(modal).toBeVisible()
  await expect(page.locator('.clip-card[data-id="e2e-clip-002"]')).toHaveCount(1)
})
