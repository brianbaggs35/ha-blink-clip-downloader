import { test, expect } from './coverage-fixtures'

// The Automations tab is generator-driven now: the builders are pure
// client-side YAML assembly (covered in depth by Vitest), so what belongs
// here is the part only a real browser can show — the PrimeVue Listbox/
// Tabs/number-input interactions actually re-rendering the preview, the
// clipboard round trip, and the Notification Channels card's real test
// endpoints, which are safe to click because nothing is configured in the
// standalone server, so each one fails gracefully rather than sending
// anything anywhere.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="automations"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="automations"]')
})

test('opens on the automation builder with a generated automation', async ({ page }) => {
  await expect(page.getByRole('heading', { name: 'Home Assistant' })).toBeVisible()
  await expect(page.locator('.recipe-title')).toContainText('Suspicious clip alert')
  await expect(page.locator('.code-block')).toContainText('event_type: blink_clip_analyzed')
})

test('changing a threshold regenerates the YAML', async ({ page }) => {
  await page.locator('.recipe-listbox').getByText('Cloud backup storage is filling up').click()
  const preview = page.locator('.code-block')
  await expect(preview).toContainText('above: 85')

  const threshold = page.locator('#recipe-field-threshold')
  await threshold.fill('92')
  await threshold.blur()

  await expect(preview).toContainText('above: 92')
  await expect(preview).toContainText('entity_id: sensor.blink_cloud_storage')
})

test('switching recipes swaps both the form and the preview', async ({ page }) => {
  await page.locator('.recipe-listbox').getByText('Daily summary').click()
  await expect(page.locator('.code-block')).toContainText('trigger: time')
  // And the switch resets the form to that recipe's own fields.
  await expect(page.locator('#recipe-field-notify_service')).toHaveValue('notify.notify')
})

test('a camera filter uses the real camera list from the backend', async ({ page }) => {
  // Seeded cameras come from the standalone server's own clip library.
  await page.locator('.recipe-listbox').getByText('Notify on a new clip').click()
  // The widget itself, not its id: MultiSelect puts inputId on its hidden
  // input, which is not what a user clicks.
  await page.locator('.recipe-field-cameras .p-multiselect').click()
  await page.getByRole('option', { name: 'Front Door' }).click()
  await page.keyboard.press('Escape')
  await expect(page.locator('.code-block')).toContainText('trigger.event.data.camera in ["Front Door"]')
})

test('a toggle adds the snapshot attachment to the generated notification', async ({ page }) => {
  const preview = page.locator('.code-block')
  await expect(preview).not.toContainText('camera_proxy')

  // ToggleSwitch renders a real checkbox input behind its track, so the
  // label's id is what a test can reliably flip.
  await page.locator('.recipe-field-snapshot').getByRole('switch').click()

  await expect(preview).toContainText('/api/camera_proxy/camera.blink_')
})

test('the Scripts & Helpers tab generates a rest_command against the add-on', async ({ page }) => {
  await page.getByRole('tab', { name: 'Scripts & Helpers' }).click()
  await expect(page.locator('.code-block')).toContainText('rest_command:')
  await expect(page.locator('.code-block')).toContainText('/api/download-now')
})

test('the Scripts & Helpers picker reaches its scenes and helpers too', async ({ page }) => {
  await page.getByRole('tab', { name: 'Scripts & Helpers' }).click()
  await page.locator('.recipe-listbox').getByText('Security alert lighting scene').click()
  await expect(page.locator('.code-block')).toContainText('id: blink_security_alert')

  await page.locator('.recipe-listbox').getByText('A switch that pauses Blink alerts').click()
  const preview = page.locator('.code-block')
  await expect(preview).toContainText('input_boolean:')
  // Its un-pause automation is opt-out, not opt-in — a forgotten toggle
  // silently disabling every alert is what that default is protecting.
  await expect(preview).toContainText('input_boolean.turn_off')
})

test('the Dashboards tab generates camera URLs and a Lovelace view', async ({ page }) => {
  await page.getByRole('tab', { name: 'Dashboards' }).click()
  const blocks = page.locator('.code-block')
  await expect(blocks.first()).toContainText('/api/security-feed/snapshot/')
  await expect(blocks.nth(1)).toContainText('type: picture-entity')

  // The iframe route needs no Home Assistant setup at all, so it drops the
  // camera-entity step entirely.
  await page.locator('.p-selectbutton').getByText('Embed this tab', { exact: true }).click()
  await expect(blocks.first()).toContainText('type: iframe')
  await expect(blocks.first()).toContainText('kiosk=1&tab=securityfeed')
})

test('the Blueprints tab offers importable blueprint YAML', async ({ page }) => {
  await page.getByRole('tab', { name: 'Blueprints' }).click()
  await expect(page.locator('.code-block').first()).toContainText('domain: automation')
  await expect(page.getByText('config/blueprints/automation')).toBeVisible()
})

test('the reference tab lists the entities the builders write against', async ({ page }) => {
  await page.getByRole('tab', { name: 'Entities & Events' }).click()
  await expect(page.getByText('sensor.blink_cloud_storage').first()).toBeVisible()
  await expect(page.getByText('blink_camera_battery_low').first()).toBeVisible()
})

test('kiosk mode renders one tab with no navigation, for an iframe card', async ({ page }) => {
  await page.goto('/?kiosk=1&tab=securityfeed')
  await expect(page.locator('.app-nav')).toHaveCount(0)
  await expect(page.locator('#page-securityfeed')).toHaveClass(/active/)
  // The page's own chrome is hidden too — a dashboard card is not the place
  // for a settings panel.
  await expect(page.locator('.secfeed-customize')).toBeHidden()
})

test('sending a test email with no SMTP configured fails gracefully', async ({ page }) => {
  await page.getByRole('button', { name: 'Send test email' }).click()
  await expect(page.getByText('Test email failed')).toBeVisible()
})

test('sending a test Discord message with no webhook configured fails gracefully', async ({ page }) => {
  await page.getByRole('button', { name: 'Send test message' }).click()
  await expect(page.getByText('Test Discord message failed')).toBeVisible()
})

test('the mobile and Home Assistant channels also fail gracefully with nothing configured', async ({ page }) => {
  await page.getByRole('button', { name: 'Send test notification', exact: true }).click()
  await expect(page.getByText('Test push notification failed')).toBeVisible()

  await page.getByRole('button', { name: 'Send test HA notification' }).click()
  await expect(page.getByText('Test HA notification failed')).toBeVisible()
})

test('a channel test that cannot reach the backend at all says so rather than nothing', async ({ page }) => {
  // The endpoints above answer {success:false} — a *transport* failure is
  // a different branch, and the one that leaves the card with no result
  // message at all if it is not handled.
  await page.route('**/api/notifications/test-email', (route) => route.fulfill({ status: 500, body: 'boom' }))
  await page.getByRole('button', { name: 'Send test email' }).click()
  await expect(page.getByText('Test email failed')).toBeVisible()
  await expect(page.getByText('Failed to send — check the add-on logs')).toBeVisible()
})

test('copying a generated automation shows a confirmation toast', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.locator('.code-block').first().getByRole('button', { name: 'Copy' }).click()
  await expect(page.getByText('Copied to clipboard')).toBeVisible()
})

test('a generated automation downloads as a .yaml file', async ({ page }) => {
  const download = page.waitForEvent('download')
  await page.locator('.code-block').first().getByRole('button', { name: 'Download' }).click()
  expect((await download).suggestedFilename()).toBe('blink-suspicious-clip-alert.yaml')
})
