import { expect, ingressNavigationUrl, test } from './coverage-fixtures'

const ingressBase = new URL('http://homeassistant.local/api/hassio_ingress/test-token/')

test('root-relative navigation is prefixed with the ingress path', () => {
  expect(ingressNavigationUrl('/', ingressBase)).toBe('http://homeassistant.local/api/hassio_ingress/test-token/')
  expect(ingressNavigationUrl('/?clip=example', ingressBase)).toBe(
    'http://homeassistant.local/api/hassio_ingress/test-token/?clip=example',
  )
  expect(ingressNavigationUrl('/__ha_panel__', ingressBase)).toBe(
    'http://homeassistant.local/api/hassio_ingress/test-token/__ha_panel__',
  )
})

test('navigation already on ingress and direct add-on URLs are unchanged', () => {
  expect(ingressNavigationUrl('http://homeassistant.local/api/hassio_ingress/test-token/library', ingressBase)).toBe(
    'http://homeassistant.local/api/hassio_ingress/test-token/library',
  )
  expect(ingressNavigationUrl('http://homeassistant.local:8099/login', ingressBase)).toBe(
    'http://homeassistant.local:8099/login',
  )
})

test('page.goto("/") loads the app through Home Assistant ingress', async ({ page }) => {
  await page.goto('/')

  if (process.env.BLINK_E2E_HA === '1' && process.env.HA_E2E_INGRESS_URL) {
    const ingressPath = new URL(process.env.HA_E2E_INGRESS_URL).pathname
    expect(new URL(page.url()).pathname.startsWith(ingressPath)).toBe(true)
  }

  await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
})
