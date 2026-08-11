import { test, expect, type Page } from '@playwright/test'

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
