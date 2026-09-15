import { test, expect } from './coverage-fixtures'

// camera_configs.json is one file that two tabs edit through the same
// full-array PUT: the AI tab owns description/custom prompt, the Vehicles
// tab owns is_car_camera/car_zone. Each has to round-trip the fields it
// does not own, or saving from one silently wipes the other's work -- a
// data-loss bug neither component's own unit tests can see, since each
// only ever exercises its own half. Driven here through both real tabs
// with a reload in between, on a camera no other spec saves config for.
//
// Both tests restore what they changed, so the Vehicles and AI specs that
// run later see Front Door exactly as they would have.

const CAMERA = 'Front Door'

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
