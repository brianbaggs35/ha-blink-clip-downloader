import type { Locator, Page } from '@playwright/test'
import { test, expect } from './coverage-fixtures'

// The Assets tab against the real backend: every write below goes through
// media_server/assets.py to the redirected protected_assets.json and back.
// standalone_server.py seeds one asset — "Mailbox" on Front Door, with a
// real reference frame — and Test Scratch is the one camera whose newest clip
// has a real thumbnail, so between them these tests mark and edit assets on
// two different cameras. Nothing here touches a clip, so no other spec's
// counts can move. The tests run in order and share what they create; the
// last one removes it again over HTTP (never by navigating, which would throw
// that test's coverage away).

async function openTab(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="assets"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="assets"]')
}

function cameraCard(page: Page, camera: string): Locator {
  return page.locator('.asset-camera-card', { has: page.getByRole('heading', { name: `📷 ${camera}`, exact: true }) })
}

function editor(page: Page): Locator {
  return page.locator('.p-dialog.asset-editor')
}

/** Waits for the editor's frame to really decode — its size is the drawing
 * surface's size — then drags or traces across it in surface fractions. */
async function draw(page: Page, points: [number, number][]) {
  const img = editor(page).locator('.zone-canvas-image')
  await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  const surface = editor(page).getByTestId('zone-canvas-surface')
  await surface.scrollIntoViewIfNeeded()
  const box = await surface.boundingBox()
  if (!box) throw new Error('the drawing surface has no size — its frame never loaded')
  const at = ([x, y]: [number, number]) => [box.x + x * box.width, box.y + y * box.height] as const
  await page.mouse.move(...at(points[0]))
  await page.mouse.down()
  for (const point of points.slice(1)) await page.mouse.move(...at(point), { steps: 4 })
  await page.mouse.up()
}

async function chooseType(page: Page, label: string) {
  await editor(page).locator('.p-select').click()
  await page.getByRole('option', { name: label, exact: true }).click()
}

async function assetsFromApi(page: Page) {
  const response = await page.request.get('/api/assets')
  expect(response.ok()).toBe(true)
  return (await response.json()).assets as {
    id: string
    camera: string
    name: string
    enabled: boolean
    zone: { shape: string }
  }[]
}

test('every camera gets a card, and the seeded mailbox is drawn on its frame', async ({ page }) => {
  await openTab(page)
  for (const camera of ['Front Door', 'Backyard', 'Garage', 'Test Scratch']) {
    await expect(cameraCard(page, camera)).toBeVisible()
  }
  const frontDoor = cameraCard(page, 'Front Door')
  await expect(frontDoor.locator('.asset-row')).toHaveCount(1)
  await expect(frontDoor.locator('.asset-row')).toContainText('Mailbox')
  await expect(frontDoor.locator('.asset-row')).toContainText('Mailbox · right of the frame')
  await expect(frontDoor.getByRole('button', { name: 'Select Mailbox' })).toBeVisible()
  const frame = frontDoor.locator('.camera-frame-image')
  await expect.poll(() => frame.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBe(640)
  // A camera with nothing marked stays one line: no picture.
  await expect(cameraCard(page, 'Backyard')).toContainText('Nothing marked on this camera')
  await expect(cameraCard(page, 'Backyard').locator('img')).toHaveCount(0)
  await expect(page.getByText('Protecting 1 asset on 1 camera.')).toBeVisible()
})

test('marks assets on two cameras, with both tools, and all of them survive a reload', async ({ page }) => {
  await openTab(page)

  // A camera's first asset: drawn on its newest clip's frame, as a box.
  await cameraCard(page, 'Test Scratch').getByRole('button', { name: 'Mark an asset' }).click()
  await expect(editor(page)).toContainText('Mark an asset on Test Scratch')
  await chooseType(page, 'Package spot')
  const name = editor(page).getByPlaceholder('e.g. Front door')
  await expect(name).toHaveValue('Package spot')
  await expect(editor(page)).toContainText('carrying off a parcel')
  await name.fill('E2E parcels')
  await draw(page, [
    [0.15, 0.2],
    [0.55, 0.75],
  ])
  await editor(page).getByRole('button', { name: 'Save asset' }).click()
  await expect(page.getByText('“E2E parcels” is now protected')).toBeVisible()
  await expect(editor(page)).toHaveCount(0)
  await expect(cameraCard(page, 'Test Scratch').locator('.asset-row')).toContainText('E2E parcels')

  // A second camera's second asset: on its saved frame, traced freehand,
  // with the mailbox already there drawn faintly beneath.
  await cameraCard(page, 'Front Door').getByRole('button', { name: 'Mark another asset' }).click()
  await expect(editor(page).locator('.ghost .zone-label')).toHaveText('Mailbox')
  await editor(page).getByRole('button', { name: 'Freeform' }).click()
  await chooseType(page, 'Door')
  await expect(name).toHaveValue('Front door')
  await draw(page, [
    [0.14, 0.2],
    [0.3, 0.2],
    [0.3, 0.7],
    [0.14, 0.7],
  ])
  await editor(page).getByRole('button', { name: 'Save asset' }).click()
  await expect(page.getByText('“Front door” is now protected')).toBeVisible()

  await expect(page.getByText('Protecting 3 assets on 2 cameras.')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="assets"]').click()
  await expect(cameraCard(page, 'Front Door').locator('.asset-name')).toHaveText(['Mailbox', 'Front door'])
  await expect(cameraCard(page, 'Test Scratch').locator('.asset-name')).toHaveText(['E2E parcels'])

  const stored = await assetsFromApi(page)
  expect(stored.map((a) => [a.camera, a.name, a.zone.shape])).toEqual([
    ['Front Door', 'Mailbox', 'rect'],
    ['Test Scratch', 'E2E parcels', 'rect'],
    ['Front Door', 'Front door', 'polygon'],
  ])
})

test('a second asset of the same name on one camera is refused, saying why', async ({ page }) => {
  await openTab(page)
  await cameraCard(page, 'Front Door').getByRole('button', { name: 'Mark another asset' }).click()
  await chooseType(page, 'Mailbox')
  await draw(page, [
    [0.5, 0.5],
    [0.6, 0.62],
  ])
  await editor(page).getByRole('button', { name: 'Save asset' }).click()
  await expect(editor(page)).toContainText('This camera already has an asset called “Mailbox”')
  await editor(page).getByRole('button', { name: 'Cancel' }).click()
  await expect(editor(page)).toHaveCount(0)
  expect((await assetsFromApi(page)).filter((a) => a.name === 'Mailbox')).toHaveLength(1)
})

test('switching an asset off keeps it, faded, until it is switched back on', async ({ page }) => {
  await openTab(page)
  const row = cameraCard(page, 'Test Scratch').locator('.asset-row', { hasText: 'E2E parcels' })
  await row.getByRole('switch', { name: 'Watch E2E parcels' }).click()
  await expect(page.getByText('Stopped watching “E2E parcels”')).toBeVisible()
  await expect(row).toContainText('Off')
  await expect(cameraCard(page, 'Test Scratch')).toContainText('0 of 1 watched')

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="assets"]').click()
  const reloaded = cameraCard(page, 'Test Scratch').locator('.asset-row', { hasText: 'E2E parcels' })
  await expect(reloaded.getByRole('switch', { name: 'Watch E2E parcels' })).not.toBeChecked()
  await reloaded.getByRole('switch', { name: 'Watch E2E parcels' }).click()
  await expect(page.getByText('Watching “E2E parcels” again')).toBeVisible()
  expect((await assetsFromApi(page)).find((a) => a.name === 'E2E parcels')?.enabled).toBe(true)
})

test('editing renames an asset and redraws it', async ({ page }) => {
  await openTab(page)
  await cameraCard(page, 'Test Scratch').getByRole('button', { name: 'Edit E2E parcels' }).click()
  await expect(editor(page)).toContainText('Edit “E2E parcels”')
  // The camera now has a saved frame, which the edit starts on.
  await expect(editor(page).locator('.frame-tile').first()).toHaveAttribute('aria-pressed', 'true')
  await editor(page).getByPlaceholder('e.g. Front door').fill('E2E parcel shelf')
  await draw(page, [
    [0.3, 0.3],
    [0.8, 0.8],
  ])
  await editor(page).getByRole('button', { name: 'Save changes' }).click()
  await expect(page.getByText('Saved “E2E parcel shelf”')).toBeVisible()
  await expect(cameraCard(page, 'Test Scratch').locator('.asset-name')).toHaveText(['E2E parcel shelf'])
})

test('hovering a row lights its zone, and choosing a zone lights its row', async ({ page }) => {
  await openTab(page)
  const card = cameraCard(page, 'Front Door')
  await card.locator('.asset-row', { hasText: 'Front door' }).hover()
  await expect(card.locator('polygon.highlighted')).toHaveCount(1)
  await card.getByRole('button', { name: 'Select Mailbox' }).click()
  await expect(card.locator('.asset-row', { hasText: 'Mailbox' })).toHaveClass(/highlighted/)
})

test('fits a phone: nothing scrolls sideways, and the editor takes the screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openTab(page)
  const overflow = await page.locator('#page-assets').evaluate((el) => el.scrollWidth - el.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  await cameraCard(page, 'Front Door').getByRole('button', { name: 'Mark another asset' }).scrollIntoViewIfNeeded()
  await cameraCard(page, 'Front Door').getByRole('button', { name: 'Mark another asset' }).click()
  // Settled, not mid-way through its opening animation.
  await expect.poll(async () => (await editor(page).boundingBox())?.width).toBe(390)
  expect((await editor(page).boundingBox())?.height).toBe(844)
  // The form comes first on a phone, then the frame to draw on.
  const form = await editor(page).locator('.editor-form').boundingBox()
  const frame = await editor(page).locator('.editor-frame').boundingBox()
  expect(form!.y).toBeLessThan(frame!.y)
  await editor(page).getByRole('button', { name: 'Cancel' }).click()
})

test('follows the light theme as well as the dark one', async ({ page }) => {
  await openTab(page)
  const card = cameraCard(page, 'Front Door')
  const surface = () => card.evaluate((el) => getComputedStyle(el).backgroundColor)
  const dark = await surface()
  const toggle = page.getByRole('button', { name: /Switch to (light|dark) theme/ })
  await toggle.click()
  await expect.poll(surface).not.toBe(dark)
  await toggle.click()
  await expect.poll(surface).toBe(dark)
})

test('removing asks first, and a camera left with nothing goes back to one line', async ({ page }) => {
  await openTab(page)
  const card = cameraCard(page, 'Test Scratch')
  await card.getByRole('button', { name: 'Remove E2E parcel shelf' }).click()
  const confirm = page.locator('.p-dialog', { hasText: 'Remove asset?' })
  await expect(confirm).toContainText('Stop protecting “E2E parcel shelf” on Test Scratch')
  await confirm.getByRole('button', { name: 'Cancel' }).click()
  await expect(card.locator('.asset-row')).toHaveCount(1)

  await card.getByRole('button', { name: 'Remove E2E parcel shelf' }).click()
  await page.locator('.p-dialog', { hasText: 'Remove asset?' }).getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText('Removed “E2E parcel shelf”')).toBeVisible()
  await expect(card).toContainText('Nothing marked on this camera')

  // Back to what standalone_server.py seeded, over HTTP.
  for (const asset of await assetsFromApi(page)) {
    if (asset.name !== 'Mailbox') await page.request.delete(`/api/assets/${asset.id}`)
  }
  expect((await assetsFromApi(page)).map((a) => a.name)).toEqual(['Mailbox'])
})
