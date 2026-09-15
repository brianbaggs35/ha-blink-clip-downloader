import { test, expect } from './coverage-fixtures'

// The headline on the Sync Module tab is the one place this app states,
// in two words, whether the house is protected. Getting it wrong in the
// reassuring direction is the worst failure mode this app has, so each
// state is driven here through the real page.
//
// Same "mock the API layer, not the application" approach the rest of this
// suite uses: registered after beforeEach's own navigation, then a reload
// so the page's next fetch goes through the route.

function camera(name: string, armed: boolean | null) {
  return {
    name,
    armed,
    online: true,
    battery_state: 'ok',
    battery_level: 3,
    wifi_strength: -60,
    type: 'catalina',
  }
}

function syncModule(armed: boolean | null, cameras: ReturnType<typeof camera>[]) {
  return {
    name: 'Home',
    network_id: 11,
    serial: 'E2E-STATUS-0001',
    version: '2.13.30',
    status: 'online',
    online: true,
    armed,
    region_id: 'e2e',
    local_storage: false,
    cameras,
  }
}

/** The two-word headline, scoped to the hero -- the same words also appear
 *  on each module's own tag, which is a different claim about a different
 *  thing. */
function headline(page: import('@playwright/test').Page) {
  return page.locator('.system-hero-title')
}

async function showStatusFor(page: import('@playwright/test').Page, modules: unknown[]) {
  await page.route('**/api/sync-modules', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(modules),
    })
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('every module and every camera armed reads as System Armed', async ({ page }) => {
  await showStatusFor(page, [syncModule(true, [camera('Front Door', true), camera('Backyard', true)])])
  await expect(headline(page)).toHaveText('System Armed')
  await expect(page.locator('.pi-shield')).toBeVisible()
  await expect(page.getByText('2 of 2 cameras armed')).toBeVisible()
})

test('a module armed with one camera switched off is only Partially Armed', async ({ page }) => {
  // That one camera will not record on motion, which is a real hole in the
  // cover and must not be reported as a fully armed system.
  await showStatusFor(page, [syncModule(true, [camera('Front Door', true), camera('Backyard', false)])])
  await expect(headline(page)).toHaveText('Partially Armed')
  await expect(page.locator('.pi-exclamation-triangle')).toBeVisible()
  await expect(page.getByText('1 of 2 cameras armed')).toBeVisible()
})

test('a disarmed module reads as Disarmed whatever its cameras say', async ({ page }) => {
  // A disarmed module stops its cameras recording regardless of their own
  // motion flags, so the camera count must not contradict the headline.
  await showStatusFor(page, [syncModule(false, [camera('Front Door', true), camera('Backyard', true)])])
  await expect(headline(page)).toHaveText('Disarmed')
  await expect(page.locator('.pi-lock-open')).toBeVisible()
  await expect(page.getByText('0 of 2 cameras armed')).toBeVisible()
})

test('an unknown armed state is never reported as Disarmed', async ({ page }) => {
  // blinkpy's `arm` reads null, not false, whenever network_info has not
  // been parsed yet -- right after a reconnect, for instance. Reporting
  // that as "Disarmed" claims the house is unprotected when the truth is
  // simply not known yet; reporting it as "Armed" is worse still.
  await showStatusFor(page, [syncModule(null, [camera('Front Door', null), camera('Backyard', null)])])
  await expect(headline(page)).toHaveText('Partially Armed')
  await expect(page.locator('.pi-exclamation-triangle')).toBeVisible()
  await expect(headline(page)).not.toHaveText('Disarmed')
  await expect(headline(page)).not.toHaveText('System Armed')
})

test('one module armed and another disarmed is Partially Armed, not either extreme', async ({ page }) => {
  const second = { ...syncModule(false, [camera('Garage', false)]), name: 'Annexe', network_id: 12 }
  await showStatusFor(page, [syncModule(true, [camera('Front Door', true)]), second])
  await expect(headline(page)).toHaveText('Partially Armed')
  await expect(page.getByText('1 of 2 cameras armed')).toBeVisible()
})
