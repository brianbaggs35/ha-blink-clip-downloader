import { test, expect } from './coverage-fixtures'
import { AUTOMATION_RECIPES as AUTOMATION_CATALOGUE } from '../src/components/automations/recipes/automations'
import { SCRIPT_RECIPES as SCRIPT_CATALOGUE } from '../src/components/automations/recipes/scripts'

// The builders' own catalogues, exercised through the real UI.
//
// automations.spec.ts covers the tab's plumbing — picker, tabs, copy,
// download, kiosk. This file drives the *recipes*: every entry generates its
// YAML for real, and the options that change a recipe's shape are flipped so
// both sides of those decisions are exercised in a browser rather than only
// in Vitest.

// Derived from the catalogues themselves rather than hand-listed. The
// hand-written lists had silently fallen eight recipes behind — every
// recipe added in 6.0.5 was never built by this suite at all, so a new one
// could ship emitting `undefined` and nothing here would notice. Reading
// the real arrays means a recipe cannot be added without being exercised.
const AUTOMATION_RECIPES = AUTOMATION_CATALOGUE.map((recipe) => recipe.name)
const SCRIPT_RECIPES = SCRIPT_CATALOGUE.map((recipe) => recipe.name)

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

async function setText(page: import('@playwright/test').Page, field: string, value: string) {
  const input = page.locator(`#recipe-field-${field}`)
  await input.fill(value)
  await input.blur()
}

async function chooseSelect(page: import('@playwright/test').Page, field: string, option: string) {
  await page.locator(`.recipe-field-${field} .p-select`).click()
  await page.getByRole('option', { name: option, exact: true }).click()
}

/** MultiSelect puts its inputId on a hidden input, so click the widget. */
async function chooseMulti(page: import('@playwright/test').Page, field: string, options: string[]) {
  await page.locator(`.recipe-field-${field} .p-multiselect`).click()
  for (const option of options) await page.getByRole('option', { name: option, exact: true }).click()
  await page.keyboard.press('Escape')
}

test('the catalogues are non-empty, so the loops below cannot vacuously pass', () => {
  expect(AUTOMATION_RECIPES.length).toBeGreaterThan(12)
  expect(SCRIPT_RECIPES.length).toBeGreaterThan(6)
})

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
  await expect(preview(page)).toContainText('action: "notify.notify"')
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

test('a created automation reports the name Home Assistant lists it under', async ({ page }) => {
  // The real endpoint needs Supervisor; mock only that response so the
  // success path — which no dev environment can reach — is still covered.
  await page.route('**/api/ha/config/create', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ created: true, name: 'Blink – daily summary' }),
    }),
  )
  await openTab(page, 'Automations')
  await pick(page, 'Daily summary')
  await page.getByRole('button', { name: 'Create in Home Assistant' }).click()
  // Scoped to the success banner: the alias is also in the YAML preview.
  await expect(page.locator('.recipe-note strong')).toHaveText('Blink – daily summary')
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

// The options below belong to recipes that only became reachable when the
// lists above started coming from the catalogue, plus the three optional
// fields the two notification recipes share. Each flips a decision that
// changes the emitted YAML, so the assertion is on the change rather than
// on the recipe merely rendering.

test('the presence recipe can arm on departure without disarming on return', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Arm Blink when everyone leaves')
  await expect(preview(page)).toContainText('rest_command.blink_sync_disarm')

  await toggle(page, 'disarm_home')

  // Only the arm half is left, so the return trigger and its branch go too.
  await expect(preview(page)).not.toContainText('rest_command.blink_sync_disarm')
  await expect(preview(page)).not.toContainText('id: home')
  await expect(preview(page)).toContainText('rest_command.blink_sync_arm')
})

test('the presence recipe can announce that it armed', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Arm Blink when everyone leaves')
  await expect(preview(page)).not.toContainText('🏠 Blink armed')

  await toggle(page, 'notify_on_arm')

  await expect(preview(page)).toContainText('🏠 Blink armed')
  await expect(preview(page)).toContainText('Everyone is out')
})

test('the siren recipe can set off the alarm panel as well, and follows the armed state chosen', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Sound the siren when the alarm is armed')
  await expect(preview(page)).not.toContainText('alarm_control_panel.alarm_trigger')

  await toggle(page, 'trigger_alarm')
  await expect(preview(page)).toContainText('alarm_control_panel.alarm_trigger')

  // "Armed away" is the default; any other choice has to reach the template.
  await chooseSelect(page, 'armed_state', 'Armed home')
  await expect(preview(page)).toContainText('armed_home')
})

test('the archive-on-full recipe can run without announcing itself', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Archive old clips when storage fills')
  await expect(preview(page)).toContainText('notify.notify')

  await toggle(page, 'notify_after')

  await expect(preview(page)).not.toContainText('notify.notify')
  await expect(preview(page)).toContainText('rest_command')
})

test('the battery to-do recipe targets the list it is pointed at', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Add a to-do when a battery goes low')
  await setText(page, 'todo_entity', 'todo.house_jobs')
  await expect(preview(page)).toContainText('todo.house_jobs')
})

test('a notification can attach a click-through path and honour a pause switch', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Suspicious clip alert')
  await expect(preview(page)).not.toContainText('input_boolean.blink_alerts_paused')

  await setText(page, 'click_path', '/hassio/ingress/e2e_blink')
  await setText(page, 'pause_entity', 'input_boolean.blink_alerts_paused')

  await expect(preview(page)).toContainText('/hassio/ingress/e2e_blink')
  // The pause helper gates the whole automation, so it lands as a condition.
  await expect(preview(page)).toContainText('input_boolean.blink_alerts_paused')
  await expect(preview(page)).toContainText('state: "off"')
})

test('a new-clip notification can be limited to chosen sources and a time window', async ({ page }) => {
  await openTab(page, 'Automations')
  await pick(page, 'Notify on a new clip')
  await expect(preview(page)).not.toContainText('condition: time')

  await chooseMulti(page, 'sources', ['Motion (pir)', 'Sync Module storage'])
  await expect(preview(page)).toContainText('trigger.event.data.source in ["pir", "local_storage"]')

  await setText(page, 'after', '21:30')
  await setText(page, 'before', '06:00')
  await expect(preview(page)).toContainText('condition: time')
  await expect(preview(page)).toContainText('21:30')
  await expect(preview(page)).toContainText('06:00')
})

test('the all-clear scene can leave the lights on instead of turning them off', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'All-clear lighting scene')
  await expect(preview(page)).toContainText('state: "off"')

  await toggle(page, 'leave_on')

  await expect(preview(page)).toContainText('state: "on"')
})

test('the template sensors take the warning and critical levels they are given', async ({ page }) => {
  await openTab(page, 'Scripts & Helpers')
  await pick(page, 'Storage health template sensors')
  await setNumber(page, 'warning', '55')
  await setNumber(page, 'critical', '77')
  await expect(preview(page)).toContainText('55')
  await expect(preview(page)).toContainText('77')
})
