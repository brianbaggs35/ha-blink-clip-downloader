import { test, expect } from './coverage-fixtures'

// Automations is mostly static reference content (no fetch on mount), but
// its Notification Channels card has real test-action buttons that hit
// real endpoints — safe to click here since none of email/Discord/mobile/
// HA notification channels are configured in the standalone server, so
// each one fails gracefully (a real round trip, not a mocked one) rather
// than actually sending anything anywhere.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="automations"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="automations"]')
})

test('renders the automation reference content', async ({ page }) => {
  await expect(page.getByText('HA Automation Examples')).toBeVisible()
  await expect(page.getByText('sensor.blink_downloader_status').first()).toBeVisible()
  await expect(page.getByText('blink_clip_downloaded').first()).toBeVisible()
})

test('sending a test email with no SMTP configured fails gracefully', async ({ page }) => {
  await page.getByRole('button', { name: 'Send test email' }).click()
  await expect(page.getByText('Test email failed')).toBeVisible()
})

test('sending a test Discord message with no webhook configured fails gracefully', async ({ page }) => {
  await page.getByRole('button', { name: 'Send test message' }).click()
  await expect(page.getByText('Test Discord message failed')).toBeVisible()
})
