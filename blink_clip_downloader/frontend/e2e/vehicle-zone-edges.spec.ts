import { test, expect } from './coverage-fixtures'

// Drawing edge cases in the Vehicles tab's zone picker. The protected-zone
// outline is the single most consequential thing a user draws in this app
// -- 6.0.0 enforces it as drawn rather than as its bounding box -- so the
// ways a draft can go wrong all need to fail safely and visibly rather
// than saving something the user did not mean.
//
// Runs after vehicles.spec.ts's own tests would (alphabetically 'vehicle-'
// sorts before 'vehicles.'), so this file leaves Test Scratch with no
// saved zone and its car-camera flag on, which is the state vehicles.spec.ts
// starts from anyway.

const CAMERA = 'Test Scratch'

async function openVehicles(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')
  return page.locator('.camera-card', { hasText: CAMERA })
}

async function setCarCamera(page: import('@playwright/test').Page, on: boolean) {
  const card = await openVehicles(page)
  const toggle = card.locator('input[role="switch"]')
  if ((await toggle.isChecked()) !== on) {
    await toggle.click()
    await page.getByRole('button', { name: 'Save Camera Settings' }).click()
    await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()
  }
}

test.beforeEach(async ({ page }) => {
  await setCarCamera(page, true)
})

// vehicles.spec.ts runs straight after this file (workers: 1, alphabetical)
// and starts from Test Scratch being an ordinary camera with no zone. This
// file has to hand it back in that state, or its first test finds a zone
// picker already on screen.
test.afterAll(async ({ browser }) => {
  const page = await browser.newPage()
  try {
    const card = await openVehicles(page)
    const clearZone = card.getByRole('button', { name: 'Clear zone' })
    if (await clearZone.count()) {
      await clearZone.click()
      await page.getByRole('button', { name: 'Confirm' }).click()
      await expect(page.getByText('Vehicle zone cleared')).toBeVisible()
    }
    await setCarCamera(page, false)
  } finally {
    await page.close()
  }
})

async function overlayBox(page: import('@playwright/test').Page, shape?: 'rect' | 'polygon') {
  const card = page.locator('.camera-card', { hasText: CAMERA })
  // A zone left over from an earlier test shows the static preview instead
  // of the drawing surface; step back into edit mode before drawing.
  const edit = card.getByRole('button', { name: 'Edit zone' })
  if (await edit.count()) await edit.click()
  // Switching tools resets the draft and re-renders the surface, so pick
  // the tool before measuring it.
  if (shape === 'polygon') await card.getByRole('button', { name: '✏️ Freeform' }).click()
  const img = card.locator('.picker-image')
  await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  const overlay = card.locator('.picker-overlay')
  await overlay.scrollIntoViewIfNeeded()
  const box = await overlay.boundingBox()
  if (!box) throw new Error('zone picker overlay has no bounding box')
  return { card, box }
}

test('a freeform scribble too small to be intentional stays unsaveable', async ({ page }) => {
  const { card, box } = await overlayBox(page, 'polygon')

  // Three points, each past the 4px minimum spacing so they are all
  // captured, but spanning only a few pixels overall -- a slip of the
  // hand, not a zone around a car.
  await page.mouse.move(box.x + 40, box.y + 40)
  await page.mouse.down()
  await page.mouse.move(box.x + 46, box.y + 41)
  await page.mouse.move(box.x + 47, box.y + 47)
  await page.mouse.up()

  // The trace clears the "at least three points" bar but not the minimum
  // span, so Save must stay disabled rather than accepting the click and
  // silently storing nothing.
  await expect(card.getByRole('button', { name: 'Save zone' })).toBeDisabled()
  // ...while Clear is still offered, so the stray marks can be wiped.
  await expect(card.getByRole('button', { name: 'Clear', exact: true })).toBeEnabled()
})

test('switching between rectangle and freeform discards the draft rather than mixing them', async ({ page }) => {
  const { card, box } = await overlayBox(page)

  // Start a perfectly good rectangle...
  await page.mouse.move(box.x + 30, box.y + 30)
  await page.mouse.down()
  await page.mouse.move(box.x + 140, box.y + 110)
  await page.mouse.up()
  await expect(card.locator('.zone-rect')).toBeVisible()

  // ...then change your mind about the tool. Keeping the rectangle here
  // would save a shape drawn with a tool the user has just left.
  await card.getByRole('button', { name: '✏️ Freeform' }).click()
  await expect(card.locator('.zone-rect')).toHaveCount(0)
  await expect(card.getByRole('button', { name: 'Save zone' })).toBeDisabled()

  await card.getByRole('button', { name: '▭ Rectangle' }).click()
  await expect(card.locator('.zone-polygon')).toHaveCount(0)
  await expect(card.getByRole('button', { name: 'Save zone' })).toBeDisabled()
})

test('a reference frame that will not load says so instead of collapsing the canvas', async ({ page }) => {
  // Without this the image silently 404s, the drawing surface collapses to
  // the height of its alt text, and the zone simply cannot be drawn with
  // nothing on screen explaining why.
  await page.route('**/api/clips/*/thumb', (route) => route.fulfill({ status: 404, body: '' }))
  await page.route('**/api/vehicle/zone-snapshot/**', (route) => route.fulfill({ status: 404, body: '' }))
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  const card = page.locator('.camera-card', { hasText: CAMERA })
  const edit = card.getByRole('button', { name: 'Edit zone' })
  if (await edit.count()) await edit.click()
  await expect(card.getByText('This clip has no stored thumbnail')).toBeVisible()
})
