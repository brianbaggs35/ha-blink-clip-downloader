import { test, expect } from './coverage-fixtures'

// An alert's "open the clip" link (composables/useClipDeepLink.ts), against
// real seeded clips: e2e-clip-000 is on Front Door, e2e-clip-001 on
// Backyard. Read-only — nothing here changes a clip.

test('a ?clip= link opens that clip straight away', async ({ page }) => {
  await page.goto('/?clip=e2e-clip-000')

  const modal = page.locator('.modal-bg.open')
  await expect(modal.locator('.modal-title')).toContainText('Front Door')
  await expect(modal.locator('.meta-grid')).toContainText('pir')
  await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
})

// Home Assistant 2026.2+ loads the add-on at its own root inside an iframe
// and tells it the route after the slug only when asked. This stands in for
// Home Assistant's panel on the same origin, as ingress is, speaking the
// same two messages its ha-panel-app does — so the whole handshake runs in
// a real browser, not only the part a unit test can reach.
const PANEL = `<!doctype html>
<html><body style="margin:0">
<iframe id="app" src="/" style="width:1280px;height:800px;border:0"></iframe>
<script>
  const frame = document.getElementById('app')
  window.subscriptions = 0
  window.route = { prefix: '/app/local_blink_clip_downloader', path: '/clip/e2e-clip-000' }
  window.sendRoute = () =>
    frame.contentWindow.postMessage({ type: 'home-assistant/properties', narrow: false, route: window.route }, '*')
  window.addEventListener('message', (event) => {
    if (event.source !== frame.contentWindow) return
    if (event.data && event.data.type === 'home-assistant/subscribe-properties') {
      window.subscriptions += 1
      window.sendRoute()
    }
  })
</script>
</body></html>`

test('inside Home Assistant, the panel route opens the clip, and a later alert opens its own', async ({ page }) => {
  await page.route('**/__ha_panel__', (route) => route.fulfill({ status: 200, contentType: 'text/html', body: PANEL }))
  await page.goto('/__ha_panel__')

  const app = page.frameLocator('#app')
  const modal = app.locator('.modal-bg.open')
  await expect(modal.locator('.modal-title')).toContainText('Front Door')
  expect(await page.evaluate(() => (window as unknown as { subscriptions: number }).subscriptions)).toBe(1)

  // Closing the clip sticks: Home Assistant resends the same route whenever
  // anything changes (a rotated phone), and that must not reopen it.
  await modal.locator('.modal-close').click()
  await expect(app.locator('.modal-bg.open')).toHaveCount(0)
  await page.evaluate(() => (window as unknown as { sendRoute: () => void }).sendRoute())
  await page.waitForTimeout(300)
  await expect(app.locator('.modal-bg.open')).toHaveCount(0)

  // A second alert tapped while the panel is open changes the route.
  await page.evaluate(() => {
    const w = window as unknown as { route: { path: string }; sendRoute: () => void }
    w.route.path = '/clip/e2e-clip-001'
    w.sendRoute()
  })
  await expect(app.locator('.modal-bg.open .modal-title')).toContainText('Backyard')

  // coverage-fixtures reads the top page's counters, but the app ran in the
  // frame; hand them up (same origin) so this test's coverage is kept.
  await page.evaluate(() => {
    const frame = document.getElementById('app') as HTMLIFrameElement
    const inner = frame.contentWindow as unknown as { __coverage__?: unknown }
    ;(window as unknown as { __coverage__?: unknown }).__coverage__ = inner.__coverage__
  })
})
