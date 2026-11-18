import { appApiUrl, test, expect } from './coverage-fixtures'

// Scheduled arming through the real server: the Sync Module tab saves the list
// to the add-on, which keeps it in /data. The scheduler itself is not started
// in the standalone backend (nothing may fire in the middle of a run), so this
// covers what a person can do -- add, edit, switch off, delete, and read the
// list back after a reload -- not the clock.

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
})

// Over HTTP, not by driving the UI: a navigation here would also throw away
// the coverage this test collected.
test.afterEach(async ({ page }) => {
  await page.request.put(appApiUrl('/api/sync-modules/schedules'), { data: { schedules: [] } })
})

test('adds a nightly arm for the whole system and keeps it across a reload @standalone', async ({ page }) => {
  const card = page.getByTestId('arming-schedules')
  await expect(card.getByText('No schedules yet')).toBeVisible()
  await expect(card.getByText(/Times are read on the add-on's own clock/)).toBeVisible()

  await card.getByRole('button', { name: 'Add schedule' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('Bedtime').fill('Bedtime')
  await dialog.locator('input[type="time"]').fill('22:30')
  await dialog.getByRole('button', { name: 'Weekdays' }).click()
  await dialog.getByRole('button', { name: 'Save' }).click()

  await expect(page.getByText('Schedule saved')).toBeVisible()
  const row = card.locator('.as-row', { hasText: 'Bedtime' })
  await expect(row).toContainText('Arm Entire system at 22:30 · Weekdays')
  await expect(row).toContainText('Has not run yet')

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await expect(page.getByTestId('arming-schedules').locator('.as-row', { hasText: 'Bedtime' })).toContainText(
    'Arm Entire system at 22:30 · Weekdays',
  )
})

test('switches a schedule off, edits it to disarm one sync module, then deletes it @standalone', async ({ page }) => {
  const card = page.getByTestId('arming-schedules')
  await card.getByRole('button', { name: 'Add schedule' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click()
  const row = card.locator('.as-row')
  await expect(row).toContainText('Arm Entire system at 22:00')
  await expect(row).toContainText('Every day')

  await row.locator('input[role="switch"]').click()
  await expect(row).toHaveClass(/as-off/)

  await row.getByRole('button', { name: /^Edit / }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByText('Disarm', { exact: true }).click()
  await dialog.locator('.p-select').click()
  await page.getByRole('option', { name: 'Home' }).click()
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(row).toContainText('Disarm Home at 22:00')

  await row.getByRole('button', { name: /^Delete / }).click()
  await page.locator('.modal-bg.open').getByRole('button', { name: 'Confirm' }).click()
  await expect(card.getByText('No schedules yet')).toBeVisible()
})
