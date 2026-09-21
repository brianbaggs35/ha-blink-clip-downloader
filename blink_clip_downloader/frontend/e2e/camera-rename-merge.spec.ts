import { test, expect } from './coverage-fixtures'
import type { Page, Route } from '@playwright/test'

// Both the AI tab and the Vehicles tab save camera configs by re-reading
// the server's copy and merging their own fields onto it. If the camera
// was renamed in the Blink app between the page loading and the save, the
// row the user edited no longer exists under that name -- so each tab
// works out which newly-appeared camera is the renamed one, using the
// alias map the server returns alongside the configs, and carries the
// edits across.
//
// Getting that wrong produces this repo's most-repeated bug in its most
// annoying form: the edited camera appears twice, once under the new name
// with default settings and once under the old name with the settings and
// no clips. Neither tab's half of that logic had a test.
//
// The PUT is intercepted rather than allowed through, so the shared
// database keeps whatever it had -- what matters is the body the client
// decided to send.

type Config = Record<string, unknown> & { camera: string }

/**
 * Make the next configs read report *from* renamed *to* newName, with the
 * alias header the server would send, and capture the resulting PUT.
 */
function renameOnReread(page: Page, from: string, to: string) {
  const captured: { body: Config[] | null } = { body: null }
  void page.route('**/api/ai/camera-configs', async (route: Route) => {
    const method = route.request().method()
    if (method === 'PUT') {
      captured.body = route.request().postDataJSON() as Config[]
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ saved: true, count: captured.body.length }),
      })
      return
    }
    if (method !== 'GET') {
      await route.fallback()
      return
    }
    const response = await route.fetch()
    const configs = (await response.json()) as Config[]
    await route.fulfill({
      response,
      headers: {
        ...response.headers(),
        'x-camera-aliases': JSON.stringify({ [from.toLowerCase()]: to }),
      },
      json: configs.map((config) => (config.camera === from ? { ...config, camera: to } : config)),
    })
  })
  return captured
}

const CAMERA = 'Test Scratch'
const RENAMED = 'Test Scratch Renamed'

test('a Vehicles-tab save follows a camera renamed underneath it', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')

  const card = page.locator('.camera-card', { hasText: CAMERA })
  await card.locator('input[role="switch"]').click()
  await expect(card.locator('.zone-picker')).toBeVisible()

  // The rename only becomes visible on the re-read the save itself does.
  const captured = renameOnReread(page, CAMERA, RENAMED)
  await page.getByRole('button', { name: 'Save Camera Settings' }).click()
  await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()

  const body = captured.body ?? []
  const renamed = body.find((config) => config.camera === RENAMED)
  expect(renamed, 'the renamed camera must be in the saved array').toBeTruthy()
  // The edit followed the rename rather than being left behind.
  expect(renamed?.is_car_camera).toBe(true)
  // And the old name was not re-added alongside it.
  expect(body.filter((config) => config.camera === CAMERA)).toHaveLength(0)
})

test('an AI-tab save follows a camera renamed underneath it', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await page.locator('.p-accordionheader', { hasText: CAMERA }).click()

  const typed = 'Description written just before the camera was renamed'
  await page.locator(`[id="cam-desc-${CAMERA}"]`).fill(typed)

  const captured = renameOnReread(page, CAMERA, RENAMED)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()

  const body = captured.body ?? []
  const renamed = body.find((config) => config.camera === RENAMED)
  expect(renamed?.description).toBe(typed)
  expect(body.filter((config) => config.camera === CAMERA)).toHaveLength(0)
})

test('two cameras changing at once is not guessed at as a rename', async ({ page }) => {
  // The inference only fires when exactly one camera vanished and exactly
  // one appeared. With two of each there is no way to tell which became
  // which, and transplanting someone's zone onto the wrong camera is worse
  // than losing the edit -- so the edits must stay on their own names.
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')

  const card = page.locator('.camera-card', { hasText: CAMERA })
  await card.locator('input[role="switch"]').click()
  await expect(card.locator('.zone-picker')).toBeVisible()

  const captured: { body: Config[] | null } = { body: null }
  await page.route('**/api/ai/camera-configs', async (route: Route) => {
    const method = route.request().method()
    if (method === 'PUT') {
      captured.body = route.request().postDataJSON() as Config[]
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"saved": true, "count": 0}' })
      return
    }
    if (method !== 'GET') {
      await route.fallback()
      return
    }
    const response = await route.fetch()
    const configs = (await response.json()) as Config[]
    // Two renames at once, and no alias map to disambiguate them.
    await route.fulfill({
      response,
      json: configs.map((config) =>
        config.camera === CAMERA
          ? { ...config, camera: 'Mystery One' }
          : config.camera === 'Front Door'
            ? { ...config, camera: 'Mystery Two' }
            : config,
      ),
    })
  })

  await page.getByRole('button', { name: 'Save Camera Settings' }).click()
  await expect(page.getByText('Vehicle camera settings saved')).toBeVisible()

  const body = captured.body ?? []
  // Neither mystery camera inherited the edit...
  expect(body.find((config) => config.camera === 'Mystery One')?.is_car_camera).toBeFalsy()
  expect(body.find((config) => config.camera === 'Mystery Two')?.is_car_camera).toBeFalsy()
  // ...and the edited camera is kept under its own name rather than dropped.
  expect(body.find((config) => config.camera === CAMERA)?.is_car_camera).toBe(true)
})
