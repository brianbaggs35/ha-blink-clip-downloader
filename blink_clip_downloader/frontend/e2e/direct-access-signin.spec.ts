import { test, expect } from './coverage-fixtures'

// Direct Access Sign-In, against the second server scripts/standalone_server.py
// starts on the main port + 1 with the option on. Its sign-in check stands in
// for Supervisor's /auth (e2e-user / e2e-password); the form, cookie, gate
// and access token are the real code in media_server/access.py.
//
// Every other spec runs against the main server, where sign-in is off — the
// same as an add-on with the option turned off — so nothing here can change
// what they see.
const SIGNIN_URL = 'http://localhost:8200'

async function signIn(page: import('@playwright/test').Page, next = '/') {
  await page.goto(`${SIGNIN_URL}${next}`)
  await expect(page).toHaveURL(/\/login\?next=/)
  await page.getByLabel('Username').fill('e2e-user')
  await page.getByLabel('Password').fill('e2e-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
}

test('the direct port asks for a Home Assistant sign-in before showing anything', async ({ page, request }) => {
  // The API refuses outright rather than redirecting, so a script sees why.
  const api = await request.get(`${SIGNIN_URL}/api/clips`)
  expect(api.status()).toBe(401)
  expect((await api.json()).login_required).toBe(true)

  await page.goto(`${SIGNIN_URL}/?tab=status`)
  await expect(page).toHaveURL(`${SIGNIN_URL}/login?next=%2F%3Ftab%3Dstatus`)
  await expect(page.getByRole('heading', { name: 'Blink Clips' })).toBeVisible()
  await expect(page.getByText('Sign in with your Home Assistant account')).toBeVisible()

  await page.getByLabel('Username').fill('e2e-user')
  await page.getByLabel('Password').fill('not-the-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText("That username and password didn't match a Home Assistant user.")
  // The username is kept, so only the password needs typing again.
  await expect(page.getByLabel('Username')).toHaveValue('e2e-user')

  await page.getByLabel('Password').fill('e2e-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(`${SIGNIN_URL}/?tab=status`)
  await expect(page.locator('.app-nav-tab.active[data-tab="status"]')).toBeVisible()

  // Signed in on the direct port, so there is something to sign out of.
  await page.getByTestId('sign-out').click()
  await expect(page).toHaveURL(`${SIGNIN_URL}/login`)
  await page.goto(`${SIGNIN_URL}/`)
  await expect(page).toHaveURL(/\/login\?next=/)
})

test('the Automations tab puts the access token into what Home Assistant will call', async ({ page, request }) => {
  await signIn(page)
  await expect(page.locator('.app-nav')).toBeVisible()
  await page.locator('.app-nav-tab[data-tab="automations"]').click()

  const card = page.getByTestId('access-token-card')
  await expect(card).toBeVisible()
  const field = card.locator('#access-token')
  await expect(field).not.toHaveValue(/[A-Za-z0-9_-]{20,}/)
  await card.getByTestId('access-token-toggle').click()
  const token = await field.inputValue()
  expect(token).toMatch(/^[A-Za-z0-9_-]{20,}$/)

  // A rest_command carries it as a bearer header...
  await page.getByRole('tab', { name: 'Scripts & Helpers' }).click()
  await page.locator('.recipe-listbox').getByText('Sync clips now', { exact: true }).click()
  await expect(page.locator('.code-block')).toContainText(`Authorization: "Bearer ${token}"`)

  // ...and it opens that endpoint, but nothing else, with no sign-in at all.
  const headers = { Authorization: `Bearer ${token}` }
  expect((await request.post(`${SIGNIN_URL}/api/download-now`, { headers })).status()).toBe(200)
  expect((await request.get(`${SIGNIN_URL}/api/clips`, { headers })).status()).toBe(403)
  const snapshot = await request.get(`${SIGNIN_URL}/api/security-feed/snapshot/Front%20Door?token=${token}`)
  expect(snapshot.status()).toBe(200)
  expect(snapshot.headers()['content-type']).toContain('image/jpeg')

  // Regenerating stops the old one working and updates the YAML in place.
  await card.getByTestId('access-token-regenerate').click()
  await page
    .locator('.p-dialog', { hasText: 'Regenerate the access token?' })
    .getByRole('button', { name: 'Confirm' })
    .click()
  await expect(field).not.toHaveValue(token)
  const newToken = await field.inputValue()
  await expect(page.locator('.code-block')).toContainText(`Authorization: "Bearer ${newToken}"`)
  expect((await request.post(`${SIGNIN_URL}/api/download-now`, { headers })).status()).toBe(401)
})

test('a dashboard card embedding the kiosk view can sign in inside its frame', async ({ page }) => {
  const response = await page.goto(`${SIGNIN_URL}/?kiosk=1&tab=securityfeed`)
  await expect(page).toHaveURL(/\/login\?next=.*&kiosk=1$/)
  // Frameable, unlike the ordinary login page, or the card would be blank.
  expect(response?.headers()['x-frame-options']).toBeUndefined()

  await page.getByLabel('Username').fill('e2e-user')
  await page.getByLabel('Password').fill('e2e-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(`${SIGNIN_URL}/?kiosk=1&tab=securityfeed`)
  // Kiosk mode: the Security Feed alone, with no navigation to sign out from.
  await expect(page.locator('.app-nav')).toHaveCount(0)
})
