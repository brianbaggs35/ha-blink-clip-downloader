import { test, expect } from './coverage-fixtures'

// The sidebar's camera list is the one thing in this app that is always
// mounted, and it owns two jobs nothing else does: keeping the Library's
// camera filter honest when the camera list itself changes underneath it,
// and telling the rest of the app to reload when it does. A camera renamed
// on the Blink side is a real, recurring source of bugs here -- every
// camera-name-keyed surface has had a version of it -- so these drive the
// rename through the real UI rather than trusting the unit tests alone.

const CAMERAS = [
  { camera: 'Front Door', total: 4 },
  { camera: 'Back Yard', total: 4 },
  { camera: 'Driveway', total: 4 },
]

/** Serve *cameras* from /api/cameras until the test changes its mind. */
async function serveCameras(page: import('@playwright/test').Page, get: () => unknown[]) {
  await page.route('**/api/cameras', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(get()),
    })
  })
}

test('clicking a sidebar camera from another tab switches to the Library and filters to it', async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')

  await page.locator('.app-nav-tab[data-tab="models"]').click()
  await expect(page.locator('.app-nav-tab.active[data-tab="models"]')).toBeVisible()

  await page.locator('.app-nav-cam[data-camera="Front Door"]').click()

  // The shortcut lives in the persistent sidebar, so a click from anywhere
  // has to land somewhere the filter is actually visible.
  await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
  await expect(page.locator('.app-nav-cam.active')).toContainText('Front Door')
  await expect(page.locator('.clip-card').first()).toContainText('Front Door')
})

test('a camera renamed on the Blink side carries the Library filter over to its new name', async ({ page }) => {
  let cameras: unknown[] = [...CAMERAS]
  await serveCameras(page, () => cameras)

  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  await page.locator('.app-nav-cam[data-camera="Back Yard"]').click()
  await expect(page.locator('.app-nav-cam.active')).toContainText('Back Yard')

  // Exactly one camera vanished and exactly one appeared: that is a rename,
  // and leaving the filter pointing at a name that no longer exists would
  // show the user an empty Library with no explanation.
  cameras = [
    { camera: 'Front Door', total: 4 },
    { camera: 'Rear Garden', total: 4 },
    { camera: 'Driveway', total: 4 },
  ]

  await expect(page.locator('.app-nav-cam.active')).toContainText('Rear Garden', { timeout: 20000 })
  await expect(page.locator('.app-nav-cam[data-camera="Back Yard"]')).toHaveCount(0)
})

test('a camera that disappears among several new ones falls back to all cameras', async ({ page }) => {
  let cameras: unknown[] = [...CAMERAS]
  await serveCameras(page, () => cameras)

  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  await page.locator('.app-nav-cam[data-camera="Back Yard"]').click()
  await expect(page.locator('.app-nav-cam.active')).toContainText('Back Yard')

  // Two new names: which (if either) is the rename is unknowable, so
  // guessing one would silently show the wrong camera's clips.
  cameras = [
    { camera: 'Front Door', total: 4 },
    { camera: 'Side Gate', total: 2 },
    { camera: 'Rear Garden', total: 4 },
    { camera: 'Driveway', total: 4 },
  ]

  await expect(page.locator('.app-nav-cam.active')).toContainText('All Cameras', { timeout: 20000 })
})

test('the camera list going away entirely leaves the navigation at its last known state', async ({ page }) => {
  let fail = false
  await page.route('**/api/cameras', async (route) => {
    if (fail) return route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(CAMERAS),
    })
  })

  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  await expect(page.locator('.app-nav-cam[data-camera="Driveway"]')).toHaveCount(1)

  // A transient backend blip must not empty the navigation out from under
  // someone mid-task.
  fail = true
  await page.waitForTimeout(12000)
  await expect(page.locator('.app-nav-cam[data-camera="Driveway"]')).toHaveCount(1)
  await expect(page.locator('.app-nav-cam[data-camera="Front Door"]')).toHaveCount(1)
})
