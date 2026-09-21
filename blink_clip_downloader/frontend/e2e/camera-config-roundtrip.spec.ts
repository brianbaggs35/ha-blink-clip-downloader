import { test, expect } from './coverage-fixtures'

// camera_configs.json is one file that two tabs edit through the same
// full-array PUT: the AI tab owns description/custom prompt, the Vehicles
// tab owns is_car_camera/car_zone. Each has to round-trip the fields it
// does not own, or saving from one silently wipes the other's work -- a
// data-loss bug neither component's own unit tests can see, since each
// only ever exercises its own half. Driven here through both real tabs
// with a reload in between, on a camera no other spec saves config for.
//
// All four tests restore what they changed, so the Vehicles and AI specs
// that run later see these cameras exactly as they would have. (Spec files
// run in filename order under workers:1, and this file sorts before
// vehicles.spec.ts, which asserts Test Scratch starts with no zone.)
//
// The four combinations are not equally important. Losing a checkbox is an
// annoyance; losing a hand-drawn car zone is real work gone, and a custom
// prompt is the other field the AI tab owns -- so both of those get a test
// of their own below rather than being assumed from the description case.

const CAMERA = 'Front Door'
// Zone drawing needs a real decoded background image, which only the
// camera with a real ffmpeg-generated fixture clip has.
const ZONE_CAMERA = 'Test Scratch'

const fieldId = (prefix: string, camera: string) => `[id="${prefix}-${camera}"]`

async function gotoTab(page: import('@playwright/test').Page, tab: string) {
  await page.locator(`.app-nav-tab[data-tab="${tab}"]`).click()
  await page.waitForSelector(`.app-nav-tab.active[data-tab="${tab}"]`)
}

async function setCarCamera(page: import('@playwright/test').Page, on: boolean) {
  await gotoTab(page, 'vehicles')
  const card = page.locator('.camera-card', { hasText: CAMERA })
  await expect(card).toBeVisible()
  const toggle = card.locator('input[role="switch"]')
  if ((await toggle.isChecked()) !== on) await toggle.click()
  await expect(toggle).toBeChecked({ checked: on })
  await page.getByRole('button', { name: 'Save Camera Settings' }).click()
  await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()
}

async function setDescription(page: import('@playwright/test').Page, text: string) {
  await gotoTab(page, 'ai')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()
  await page.locator(`#cam-desc-${CAMERA.replace(' ', '\\ ')}`).fill(text)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()
}

// Same save, but for whichever camera/field is named -- the two tests below
// need Test Scratch and the custom-prompt box, which setDescription's
// Front-Door/description pairing cannot express.
async function saveAiField(
  page: import('@playwright/test').Page,
  camera: string,
  prefix: 'cam-desc' | 'cam-prompt',
  text: string,
) {
  await gotoTab(page, 'ai')
  await page.locator('.p-accordionheader', { hasText: camera }).click()
  const box = page.locator(fieldId(prefix, camera))
  await expect(box).toBeVisible()
  await box.fill(text)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()
}

async function setCarCameraFor(page: import('@playwright/test').Page, camera: string, on: boolean) {
  await gotoTab(page, 'vehicles')
  const card = page.locator('.camera-card', { hasText: camera })
  await expect(card).toBeVisible()
  const toggle = card.locator('input[role="switch"]')
  if ((await toggle.isChecked()) !== on) await toggle.click()
  await expect(toggle).toBeChecked({ checked: on })
  await page.getByRole('button', { name: 'Save Camera Settings' }).click()
  await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()
}

/** Draw and save a rectangle zone on *camera*'s picker. */
async function drawAndSaveZone(page: import('@playwright/test').Page, camera: string) {
  const card = page.locator('.camera-card', { hasText: camera })
  // The overlay is sized from the background image's natural size, so
  // measuring before it has decoded yields a zero-height box and the drag
  // lands nowhere -- same reason vehicles.spec.ts polls for naturalWidth.
  const img = card.locator('.picker-image')
  await expect(img).toBeVisible()
  await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)

  const overlay = card.locator('.picker-overlay')
  // page.mouse uses raw viewport coordinates and will not auto-scroll.
  await overlay.scrollIntoViewIfNeeded()
  const box = await overlay.boundingBox()
  if (!box) throw new Error('zone picker overlay has no bounding box - background image did not load')

  await page.mouse.move(box.x + 20, box.y + 20)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width - 20, box.y + box.height - 20, { steps: 5 })
  await page.mouse.up()
  await expect(card.locator('.zone-rect')).toBeVisible()

  await card.getByRole('button', { name: 'Save zone' }).click()
  await expect(page.getByText('Vehicle zone saved')).toBeVisible()
}

/** Put *camera* back to "not a car camera, no zone" for the specs after us. */
async function clearZoneAndCarCamera(page: import('@playwright/test').Page, camera: string) {
  await gotoTab(page, 'vehicles')
  const card = page.locator('.camera-card', { hasText: camera })
  const clear = card.getByRole('button', { name: 'Clear zone' })
  if (await clear.isVisible()) {
    await clear.click()
    await page.getByRole('button', { name: 'Confirm' }).click()
    await expect(page.getByText('Vehicle zone cleared')).toBeVisible()
  }
  await setCarCameraFor(page, camera, false)
  // Assert the restore actually happened, so a failure here is reported
  // against this spec rather than surfacing later as a confusing failure
  // in vehicles.spec.ts.
  await expect(card.locator('.zone-picker')).toHaveCount(0)
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('saving a description from the AI tab keeps what the Vehicles tab set', async ({ page }) => {
  await setCarCamera(page, true)
  await setDescription(page, 'Round-trip probe: written from the AI tab')

  await page.reload()
  await gotoTab(page, 'vehicles')
  const card = page.locator('.camera-card', { hasText: CAMERA })
  await expect(card.locator('input[role="switch"]')).toBeChecked()

  await setCarCamera(page, false)
  await setDescription(page, '')
})

test('saving from the Vehicles tab keeps the description the AI tab wrote', async ({ page }) => {
  const typed = 'Round-trip probe: owned by the AI tab'
  await setDescription(page, typed)
  await setCarCamera(page, true)

  await page.reload()
  await gotoTab(page, 'ai')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()
  await expect(page.locator(`#cam-desc-${CAMERA.replace(' ', '\\ ')}`)).toHaveValue(typed)

  await setCarCamera(page, false)
  await setDescription(page, '')
})

test('saving from the AI tab keeps a car zone the Vehicles tab drew', async ({ page }) => {
  // The expensive half of the contract: a zone is drawn by hand, and the AI
  // tab's full-array PUT does not own that field. If it fails to round-trip
  // it, the work is gone with no error shown anywhere.
  await setCarCameraFor(page, ZONE_CAMERA, true)
  await drawAndSaveZone(page, ZONE_CAMERA)

  await saveAiField(page, ZONE_CAMERA, 'cam-desc', 'Round-trip probe: must not wipe the zone')

  await page.reload()
  await gotoTab(page, 'vehicles')
  const card = page.locator('.camera-card', { hasText: ZONE_CAMERA })
  await expect(card.locator('.zone-rect')).toBeVisible()
  await expect(card.locator('input[role="switch"]')).toBeChecked()

  await saveAiField(page, ZONE_CAMERA, 'cam-desc', '')
  await clearZoneAndCarCamera(page, ZONE_CAMERA)
})

test('saving from the Vehicles tab keeps the custom prompt the AI tab wrote', async ({ page }) => {
  // custom_prompt is the AI tab's *other* field, and nothing else covers
  // it: the description passing through proves the request carries some
  // unowned fields, not that it carries this one.
  const prompt = 'Round-trip probe: custom prompt owned by the AI tab'
  await saveAiField(page, CAMERA, 'cam-prompt', prompt)
  await setCarCameraFor(page, CAMERA, true)

  await page.reload()
  await gotoTab(page, 'ai')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()
  await expect(page.locator(fieldId('cam-prompt', CAMERA))).toHaveValue(prompt)

  await setCarCameraFor(page, CAMERA, false)
  await saveAiField(page, CAMERA, 'cam-prompt', '')
})
