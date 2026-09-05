import { test, expect } from './coverage-fixtures'

const AUTH_ENDPOINT = '/api/auth/2fa'

test.beforeEach(async ({ page }) => {
  // Any six-digit code other than the simulated valid/error codes puts the
  // standalone server into its needs_2fa state without adding a test-only
  // control endpoint.
  await page.request.post(AUTH_ENDPOINT, { data: { code: '000000' } })
})

test.afterEach(async ({ page }) => {
  // Leave the shared backend connected for the next spec file.
  await page.request.post(AUTH_ENDPOINT, { data: { code: '123456' } })
})

test('shows 2FA, rejects a wrong code, then accepts a valid code', async ({ page }) => {
  await page.goto('/')

  const modal = page.locator('.modal-bg.open', { hasText: 'Two-Factor Authentication' })
  await expect(modal).toBeVisible()

  await modal.locator('#two-fa-code').fill('12')
  await modal.getByRole('button', { name: 'Verify' }).click()
  await expect(modal).toContainText('Please enter exactly 6 digits.')

  await modal.locator('#two-fa-code').fill('111111')
  await modal.getByRole('button', { name: 'Verify' }).click()
  await expect(modal).toContainText('Incorrect verification code. Please try again.', {
    timeout: 5000,
  })

  await modal.locator('#two-fa-code').fill('123456')
  await modal.getByRole('button', { name: 'Verify' }).click()
  await expect(modal).toBeHidden({ timeout: 5000 })
  await expect(page.getByText('Signed in to Blink')).toBeVisible()
})

test('shows and dismisses an authentication error banner', async ({ page }) => {
  // The fake server reserves 999999 for a deterministic full-auth failure.
  await page.request.post(AUTH_ENDPOINT, { data: { code: '999999' } })
  await page.goto('/')

  const banner = page.locator('.auth-error-banner.show')
  await expect(banner).toContainText('Blink authentication failed (simulated E2E error).')
  await banner.getByRole('button', { name: 'Dismiss' }).click()
  await expect(page.locator('.auth-error-banner.show')).toHaveCount(0)
})
