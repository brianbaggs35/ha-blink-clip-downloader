import { test, expect } from './coverage-fixtures'

const UNRELATED_UNAUTHORIZED = { error: 'Not authorized' }

async function mockStatsUnauthorized(page: import('@playwright/test').Page, body: object) {
  await page.route('**/api/stats', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify(body),
    }),
  )
}

test('does not redirect for a different unauthorized API response @standalone', async ({ page }) => {
  await mockStatsUnauthorized(page, UNRELATED_UNAUTHORIZED)
  const unauthorizedResponse = page.waitForResponse(
    (response) => response.url().includes('/api/stats') && response.status() === 401,
  )

  await page.goto('/?tab=library')
  await unauthorizedResponse

  const url = new URL(page.url())
  expect(url.pathname).toBe('/')
  expect(url.search).toBe('?tab=library')
  await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
})
