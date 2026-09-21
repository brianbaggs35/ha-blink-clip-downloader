import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The Dashboards builder had one test, covering one combination: camera
// tiles delivered as their own YAML dashboard. Every other option it
// offers — embedding this tab instead of camera entities, pasting into an
// existing dashboard rather than creating one, the storage gauges, the
// status card, the paths and titles — changes what it emits and none was
// exercised. That matters more here than for most builders, because the
// output is pasted into someone's Lovelace config by hand: a wrong entity
// id or a missing key is a broken dashboard, not a caught exception.

// Block order depends on the tile source: in camera mode the first block
// is the Generic Camera setup sheet (step 1) and the dashboard YAML is the
// second; embedding this tab needs no camera entities, so there the
// dashboard YAML is the only block.
const blocks = (page: Page) => page.locator('.code-block')
const dashboardYaml = (page: Page) => page.locator('.code-block').nth(1)

async function openDashboards(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="automations"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="automations"]')
  await page.getByRole('tab', { name: 'Dashboards' }).click()
}

/** The card has two SelectButtons: tile source first, delivery second. */
async function chooseMode(page: Page, label: string) {
  await page.locator('.p-selectbutton').first().getByText(label, { exact: true }).click()
}
async function chooseDelivery(page: Page, label: string) {
  await page.locator('.p-selectbutton').nth(1).getByText(label, { exact: true }).click()
}

test('camera tiles pasted into an existing dashboard omit the dashboard registration', async ({ page }) => {
  await openDashboards(page)
  // The default delivery: a view to paste, with no lovelace: block and no
  // filename, because the user already has a dashboard to put it in.
  await expect(dashboardYaml(page)).toContainText('cards:')
  await expect(dashboardYaml(page)).not.toContainText('lovelace:')
})

test('embedding this tab produces an iframe card instead of camera entities', async ({ page }) => {
  await openDashboards(page)
  await chooseMode(page, 'Embed this tab')

  // The kiosk URL is what makes the embed usable — the add-on's own UI
  // without its nav chrome (see App.vue's ?kiosk=1).
  await expect(blocks(page).first()).toContainText('iframe')
  await expect(blocks(page).first()).toContainText('kiosk=1')
  await expect(blocks(page).first()).not.toContainText('camera.blink_')
})

test('the storage gauges and status card can each be left out', async ({ page }) => {
  await openDashboards(page)
  const yaml = dashboardYaml(page)
  await expect(yaml).toContainText('gauge')

  await page.locator('#dash-storage').click()
  await expect(yaml).not.toContainText('gauge')

  await page.locator('#dash-status').click()
  await expect(yaml).not.toContainText('sensor.blink_downloader_status')

  // Put both back so the page is left as the other builder tests expect.
  await page.locator('#dash-storage').click()
  await page.locator('#dash-status').click()
  await expect(yaml).toContainText('gauge')
})

test('the dashboard path, view title and view path all reach the generated YAML', async ({ page }) => {
  await openDashboards(page)
  await chooseDelivery(page, 'Its own YAML dashboard')

  await page.locator('#dash-title').fill('Cameras Upstairs')
  await page.locator('#dash-path').fill('upstairs')
  await page.locator('#dash-dashboard-path').fill('blink-upstairs')

  await expect(blocks(page).nth(1)).toContainText('Cameras Upstairs')
  await expect(blocks(page).nth(1)).toContainText('upstairs')
  await expect(page.getByText('blink-upstairs.yaml').first()).toBeVisible()
})

test('a dashboard path with no dash is given one, since Home Assistant requires it', async ({ page }) => {
  await openDashboards(page)
  await chooseDelivery(page, 'Its own YAML dashboard')

  // HA rejects a single-word URL path for a YAML dashboard, so the builder
  // appends "-dashboard" rather than emitting something that will not load.
  await page.locator('#dash-dashboard-path').fill('blink')
  await expect(page.getByText('blink-dashboard.yaml').first()).toBeVisible()
})
