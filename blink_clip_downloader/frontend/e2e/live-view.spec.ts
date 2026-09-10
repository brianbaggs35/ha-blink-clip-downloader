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

test('shows a toast when the camera list fails to load', async ({ page }) => {
  await page.route('**/api/liveview/cameras', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) }),
  )
  // beforeEach's own navigation already loaded cameras successfully before
  // this route existed -- Refresh (the shared refresh.tick LiveViewPage
  // also watches) is what re-runs loadCameras() against the now-mocked
  // endpoint, same trigger status.spec.ts/storage.spec.ts use for theirs.
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.getByText('Failed to load cameras')).toBeVisible()
})

test('shows the no-cameras message when the account has none', async ({ page }) => {
  await page.route('**/api/liveview/cameras', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ cameras: [] }) }),
  )
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(
    page.getByText('No cameras found — make sure Blink is connected and your account has at least one camera.'),
  ).toBeVisible()
})

test('adopts an already-active session on mount, without needing a camera click', async ({ page }) => {
  await page.route('**/api/liveview/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/liveview/cameras') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cameras: ['Front Door'] }),
      })
      return
    }
    if (url.pathname === '/api/liveview/status') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active: true, session_id: 'already-running', camera: 'Front Door', state: 'live' }),
      })
      return
    }
    if (url.pathname === '/api/liveview/heartbeat' && route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
      return
    }
    if (url.pathname.startsWith('/api/liveview/hls/')) {
      await route.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl', body: '#EXTM3U\n' })
      return
    }
    await route.fallback()
  })

  // A fresh navigation (not just beforeEach's, which already happened
  // before these routes existed) so onMounted's own initial status check
  // -- not a later poll -- is what adopts this pre-existing session.
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')

  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()
  await expect(page.locator('#page-liveview .video-js-wrap')).not.toHaveClass(/video-hidden/)
})

test('shows a toast when stopping the live view session fails', async ({ page }) => {
  await page.route('**/api/liveview/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/liveview/cameras') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cameras: ['Front Door'] }),
      })
      return
    }
    if (url.pathname === '/api/liveview/status') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ active: false }) })
      return
    }
    if (url.pathname === '/api/liveview/start' && method === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active: true, session_id: 'mock-session', camera: 'Front Door', state: 'live' }),
      })
      return
    }
    if (url.pathname === '/api/liveview/stop' && method === 'POST') {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'mocked' }) })
      return
    }
    if (url.pathname === '/api/liveview/heartbeat' && method === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
      return
    }
    if (url.pathname.startsWith('/api/liveview/hls/')) {
      await route.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl', body: '#EXTM3U\n' })
      return
    }
    await route.fallback()
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')
  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()

  // The component sets status to inactive locally before the backend call
  // even resolves (see stop() in LiveViewPage.vue), so the picker resets
  // either way -- the failure only shows up as this extra toast.
  await page.getByRole('button', { name: '■ Stop' }).click()
  await expect(page.getByText('Failed to stop live view')).toBeVisible()
})

test('renders and stops a mocked live session without a real Blink account', async ({ page }) => {
  let active = false
  await page.route('**/api/liveview/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/liveview/cameras') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cameras: ['Front Door'] }),
      })
      return
    }
    if (url.pathname === '/api/liveview/status') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          active
            ? { active: true, session_id: 'mock-session', camera: 'Front Door', state: 'live' }
            : { active: false },
        ),
      })
      return
    }
    if (url.pathname === '/api/liveview/start' && method === 'POST') {
      active = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active: true, session_id: 'mock-session', camera: 'Front Door', state: 'live' }),
      })
      return
    }
    if (url.pathname === '/api/liveview/stop' && method === 'POST') {
      active = false
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ stopped: true }),
      })
      return
    }
    if (url.pathname === '/api/liveview/heartbeat' && method === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      })
      return
    }
    if (url.pathname.startsWith('/api/liveview/hls/')) {
      await route.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl', body: '#EXTM3U\n' })
      return
    }
    await route.fallback()
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')
  await page.getByRole('button', { name: 'Front Door', exact: true }).click()

  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()
  await expect(page.locator('#page-liveview .video-js-wrap')).not.toHaveClass(/video-hidden/)
  await page.getByRole('button', { name: '■ Stop' }).click()
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
  await expect(page.getByRole('button', { name: '■ Stop' })).toHaveCount(0)
})

// Same "real backend for the reachable part, page.route() for the rest"
// approach as the mocked session above -- neither of these two scenarios
// (switching cameras mid-session; the server ending a session with an
// error) can happen against the fake get_camera that always fails to
// start in the first place.
test('switching cameras while a session is active tears down the old one and starts a fresh session', async ({
  page,
}) => {
  let currentCamera: string | null = null
  let sessionSeq = 0
  await page.route('**/api/liveview/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/liveview/cameras') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cameras: ['Front Door', 'Backyard'] }),
      })
      return
    }
    if (url.pathname === '/api/liveview/status') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          currentCamera
            ? { active: true, session_id: `mock-${sessionSeq}`, camera: currentCamera, state: 'live' }
            : { active: false },
        ),
      })
      return
    }
    if (url.pathname === '/api/liveview/start' && method === 'POST') {
      const body = request.postDataJSON() as { camera: string }
      currentCamera = body.camera
      sessionSeq++
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active: true, session_id: `mock-${sessionSeq}`, camera: currentCamera, state: 'live' }),
      })
      return
    }
    if (url.pathname === '/api/liveview/stop' && method === 'POST') {
      currentCamera = null
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ stopped: true }) })
      return
    }
    if (url.pathname === '/api/liveview/heartbeat' && method === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
      return
    }
    if (url.pathname.startsWith('/api/liveview/hls/')) {
      await route.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl', body: '#EXTM3U\n' })
      return
    }
    await route.fallback()
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')

  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()
  await expect.poll(() => currentCamera).toBe('Front Door')
  const firstSessionSeq = sessionSeq

  await page.getByRole('button', { name: 'Backyard', exact: true }).click()
  await expect.poll(() => currentCamera).toBe('Backyard')
  expect(sessionSeq).toBeGreaterThan(firstSessionSeq)
  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()
})

test('the server ending a session with an error surfaces it and resets to the picker', async ({ page }) => {
  let sessionState: 'live' | 'error' | null = null
  await page.route('**/api/liveview/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/liveview/cameras') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cameras: ['Front Door'] }),
      })
      return
    }
    if (url.pathname === '/api/liveview/status') {
      let body: unknown = { active: false }
      if (sessionState === 'live') {
        body = { active: true, session_id: 'mock-session', camera: 'Front Door', state: 'live' }
      } else if (sessionState === 'error') {
        body = { active: false, state: 'error', error: 'ffmpeg exited unexpectedly (simulated E2E error).' }
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      return
    }
    if (url.pathname === '/api/liveview/start' && method === 'POST') {
      sessionState = 'live'
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active: true, session_id: 'mock-session', camera: 'Front Door', state: 'live' }),
      })
      return
    }
    if (url.pathname === '/api/liveview/heartbeat' && method === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
      return
    }
    if (url.pathname.startsWith('/api/liveview/hls/')) {
      await route.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl', body: '#EXTM3U\n' })
      return
    }
    await route.fallback()
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="liveview"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="liveview"]')

  await page.getByRole('button', { name: 'Front Door', exact: true }).click()
  await expect(page.getByRole('button', { name: '■ Stop' })).toBeVisible()

  // The next 4s status poll (STATUS_POLL_INTERVAL_MS in LiveViewPage.vue)
  // picks up the server-reported error and tears the session down client-side.
  sessionState = 'error'
  await expect(page.getByText('ffmpeg exited unexpectedly (simulated E2E error).')).toBeVisible({ timeout: 6000 })
  await expect(page.getByText('Select a camera above to start watching.')).toBeVisible()
  await expect(page.getByRole('button', { name: '■ Stop' })).toHaveCount(0)
})
