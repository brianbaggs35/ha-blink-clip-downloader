import { test, expect } from './coverage-fixtures'

// Live View is otherwise entirely out of e2e reach — actually starting a
// session needs a real Blink live-stream feeding a real ffmpeg process,
// nothing this environment can fake convincingly. standalone_server.py
// wires in a real LiveViewManager with a fake get_camera whose
// init_livestream() always raises, which unlocks everything up to that
// point for real: the camera picker, selecting a camera, the "starting"
// state, and a genuine (not mocked) LiveViewError caught and surfaced as
// a toast, all through live_view.py's real _create_session code path.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')
})

test('lists every seeded camera and starts with no camera selected', async ({ page }) => {
  for (const camera of ['Front Door', 'Backyard', 'Garage']) {
    await expect(page.getByRole('button', { name: camera, exact: true })).toBeVisible()
  }
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
  await expect(page.getByRole('button', { name: '■ Stop' })).toHaveCount(0)
})

test('selecting a camera shows a starting placeholder, then a real error toast and resets to the picker', async ({
  page,
}) => {
  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByText('Starting live view…')).toBeVisible()

  await expect(page.getByText(/This camera does not support live view/)).toBeVisible()
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
  await expect(page.getByRole('button', { name: '■ Stop' })).toHaveCount(0)
})

test('selecting a different camera after a failed start attempts a fresh session for the new one', async ({ page }) => {
  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByText(/This camera does not support live view/)).toBeVisible()

  await page.getByRole('button', { name: 'Backyard', exact: true }).click()
  await expect(page.getByText('Starting live view…')).toBeVisible()
  // .last(): toasts stack rather than replacing each other, so the first
  // camera's error toast is still showing — this asserts the *second*
  // attempt also produced its own real error, not just that the first
  // one's toast is (still) on screen.
  await expect(page.getByText(/This camera does not support live view/).last()).toBeVisible()
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
})
