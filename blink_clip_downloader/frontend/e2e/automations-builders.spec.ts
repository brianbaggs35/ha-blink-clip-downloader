import { test, expect } from './coverage-fixtures'

// The builders' own catalogues, exercised through the real UI.
//
// automations.spec.ts covers the tab's plumbing — picker, tabs, copy,
// download, kiosk. This file drives the *recipes*: every entry generates its
// YAML for real, and the options that change a recipe's shape are flipped so
// both sides of those decisions are exercised in a browser rather than only
// in Vitest.

const AUTOMATION_RECIPES = [
  'Suspicious clip alert',
  'Turn on lights when something looks off',
  'Cast the camera feed to a display',
  'Announce a clip on a speaker',
  'Notify on a new clip',
  'Alert on an unusually long clip',
  'Cloud backup storage is filling up',
  'Local clip storage is filling up',
  'Cloud backups are falling behind',
  'Camera battery went low',
  'Nothing has downloaded in a while',
  'Daily summary',
]

const SCRIPT_RECIPES = [
  'Sync clips now',
  'Show the camera feed on a display',
  'Read out a storage report',
  'Security alert lighting scene',
  'A switch that pauses Blink alerts',
  'Storage health template sensors',
]

/** The generated YAML, which every recipe renders into the same block. */
const preview = (page: import('@playwright/test').Page) => page.locator('.code-block')

async function openTab(page: import('@playwright/test').Page, name: string) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="automations"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="automations"]')
  if (name !== 'Automations') await page.getByRole('tab', { name }).click()
}

async function pick(page: import('@playwright/test').Page, recipe: string) {
  await page.locator('.recipe-listbox').getByText(recipe, { exact: true }).click()
  await expect(page.locator('.recipe-title')).toContainText(recipe)
}

/** A ToggleSwitch renders a real switch behind its track. */
async function toggle(page: import('@playwright/test').Page, field: string) {
  await page.locator(`.recipe-field-${field}`).getByRole('switch').click()
}

async function setNumber(page: import('@playwright/test').Page, field: string, value: string) {
  const input = page.locator(`#recipe-field-${field}`)
  await input.fill(value)
  await input.blur()
}

test('every automation recipe generates its YAML', async ({ page }) => {
  await openTab(page, 'Automations')
  for (const recipe of AUTOMATION_RECIPES) {
    await pick(page, recipe)
    // Modern HA syntax, which is what the UI editor writes back.
    await expect(preview(page)).toContainText('triggers:')
    await expect(preview(page)).toContainText('actions:')
    await expect(preview(page)).not.toContainText('undefined')
  }
})

test('every script, scene and helper recipe generates its YAML', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  for (const recipe of SCRIPT_RECIPES) {
    await pick(page, recipe)
    await expect(preview(page)).not.toContainText('undefined')
    await expect(preview(page)).not.toContainText('Could not generate')
  }
})

test('the lighting recipe drops its auto-off and sun condition when told to', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Turn on lights when something looks off')
  await expect(preview(page)).toContainText('condition: sun')
  await expect(preview(page)).toContainText('homeassistant.turn_off')

  await toggle(page, 'after_dark')
  await setNumber(page, 'auto_off', '0')

  await expect(preview(page)).not.toContainText('condition: sun')
  await expect(preview(page)).not.toContainText('homeassistant.turn_off')
})

test('the cast recipe can leave the feed up instead of stopping it', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Cast the camera feed to a display')
  await expect(preview(page)).toContainText('media_player.turn_off')

  await setNumber(page, 'stop_after', '0')

  await expect(preview(page)).not.toContainText('media_player.turn_off')
  await expect(preview(page)).toContainText('cast.show_lovelace_view')
})

test('announcing every clip listens to a different event than announcing suspicious ones', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Announce a clip on a speaker')
  await expect(preview(page)).toContainText('event_type: blink_clip_analyzed')

  await toggle(page, 'suspicious_only')

  await expect(preview(page)).toContainText('event_type: blink_clip_downloaded')
  await expect(preview(page)).not.toContainText('is_suspicious')
})

test('the daily cloud-storage reminder can be switched off', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Cloud backup storage is filling up')
  await expect(preview(page)).toContainText('trigger: time')

  await toggle(page, 'remind_daily')

  await expect(preview(page)).not.toContainText('trigger: time')
  await expect(preview(page)).toContainText('conditions: []')
})

test('a battery alert can skip the persistent notification', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Camera battery went low')
  await expect(preview(page)).toContainText('persistent_notification.create')

  await toggle(page, 'persistent')

  await expect(preview(page)).not.toContainText('persistent_notification.create')
})

test('a high-priority alert adds both platforms push keys', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Suspicious clip alert')
  await expect(preview(page)).not.toContainText('channel: alarm')

  await toggle(page, 'critical')

  // iOS reads the first, Android the second.
  await expect(preview(page)).toContainText('critical: 1')
  await expect(preview(page)).toContainText('channel: alarm')
})

test('the storage report can be delivered as a notification instead of speech', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'Read out a storage report')
  await expect(preview(page)).toContainText('tts.speak')

  await page.locator('.recipe-field-mode .p-select').click()
  await page.getByRole('option', { name: 'A notification' }).click()

  await expect(preview(page)).not.toContainText('tts.speak')
  await expect(preview(page)).toContainText('action: notify.notify')
})

test('the alert scene can leave light colour alone, and the sync script can confirm itself', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'Security alert lighting scene')
  await expect(preview(page)).toContainText('rgb_color')

  await page.locator('.recipe-field-color .p-select').click()
  await page.getByRole('option', { name: 'Leave the colour alone' }).click()
  await expect(preview(page)).not.toContainText('rgb_color')

  await pick(page, 'Sync clips now')
  await expect(preview(page)).not.toContainText('persistent_notification')
  await toggle(page, 'confirm')
  await expect(preview(page)).toContainText('persistent_notification.create')
})

test('the pause helper can be left off until it is flipped back by hand', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'A switch that pauses Blink alerts')
  // The un-pause automation is opt-out: a forgotten toggle silently
  // disabling every alert is the failure mode this default protects.
  await expect(preview(page)).toContainText('input_boolean.turn_off')

  await setNumber(page, 'auto_resume', '0')

  await expect(preview(page)).not.toContainText('input_boolean.turn_off')
  await expect(preview(page)).toContainText('input_boolean:')
})

test('the notification test panel stays at the top, above the builders', async ({ page }) => {
  await openTab(page, 'Automations')
  const panelY = await page.locator('.notification-channels-card').boundingBox()
  const buildersY = await page.locator('.recipe-builder').boundingBox()
  expect(panelY!.y).toBeLessThan(buildersY!.y)
})

test('an automation offers to create itself in Home Assistant', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Suspicious clip alert')
  await expect(page.getByRole('button', { name: 'Create in Home Assistant' })).toBeVisible()
})

test('creating without Home Assistant behind it fails with an explanation', async ({ page }) => {
  // The standalone server has no Supervisor token, so the endpoint answers
  // 503 — a real round trip, and the path a bare container would take.
  await openTab(page, 'Automations')
  await pick(page, 'Daily summary')
  await page.getByRole('button', { name: 'Create in Home Assistant' }).click()
  await expect(page.getByText('Could not reach Home Assistant')).toBeVisible()
  await expect(page.getByText('now exists in Home Assistant')).toHaveCount(0)
})

test('a created automation reports the entity it became', async ({ page }) => {
  // The real endpoint needs Supervisor; mock only that response so the
  // success path — which no dev environment can reach — is still covered.
  await page.route('**/api/ha/config/create', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ created: true, entity_id: 'automation.blink_daily_summary' }),
    }),
  )
  await openTab(page, 'Automations')
  await pick(page, 'Daily summary')
  await page.getByRole('button', { name: 'Create in Home Assistant' }).click()
  await expect(page.getByText('automation.blink_daily_summary').first()).toBeVisible()
  await expect(page.getByText('updates that same one rather than adding another')).toBeVisible()
})

test('a copy-only recipe offers no Create button, and says where to put it', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'Sync clips now')
  // It generates a rest_command too, which no API can create.
  await expect(page.getByRole('button', { name: 'Create in Home Assistant' })).toHaveCount(0)
  await expect(page.getByText('no API to create it from here')).toBeVisible()
  await expect(page.getByText('configuration.yaml').first()).toBeVisible()
})

test('the Dashboards tab can generate a YAML dashboard of its own', async ({ page }) => {
  await openTab(page, 'Dashboards')
  await page.locator('.p-selectbutton').nth(1).getByText('Its own YAML dashboard').click()
  const blocks = page.locator('.code-block')
  await expect(blocks.nth(1)).toContainText('lovelace:')
  await expect(blocks.nth(1)).toContainText('show_in_sidebar: true')
  await expect(page.getByText('blink-cameras.yaml').first()).toBeVisible()
})
