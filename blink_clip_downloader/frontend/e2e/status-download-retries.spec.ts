import { test, expect } from './coverage-fixtures'
import type { Page, Route } from '@playwright/test'

// The Blink Connection card's two download-retry rows. download_retries
// only ever comes from app.py's real poll loop (BlinkDownloader.
// download_retry_status(), set on MediaServer.extra_status after every
// download poll), which standalone_server.py never runs -- so the real
// backend is used for the "nothing owed" state, and the real /api/stats
// response is patched with the field for the rest, the same way
// status.spec.ts reaches the Storage card.
//
// Routes are registered before the first navigation, so no reload is
// needed and nothing a test exercised is thrown away with the page.

async function openStatus(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')
  await expect(page.locator('#status-page-content')).toBeVisible()
}

async function withRetries(route: Route, retries: { retrying: number; given_up: number }) {
  const response = await route.fetch()
  const stats = (await response.json()) as Record<string, unknown>
  await route.fulfill({ response, json: { ...stats, download_retries: retries } })
}

test('shows no download-retry rows when the add-on is owed nothing', async ({ page }) => {
  await openStatus(page)

  const connectionCard = page.locator('.p-card', { hasText: 'Blink Connection' })
  await expect(connectionCard).toBeVisible()
  await expect(connectionCard.getByTestId('download-retrying')).toHaveCount(0)
  await expect(connectionCard.getByTestId('download-given-up')).toHaveCount(0)
})

test('shows clips waiting on a retry and clips given up on in the Blink Connection card', async ({ page }) => {
  await page.route('**/api/stats', (route) => withRetries(route, { retrying: 3, given_up: 1 }))

  await openStatus(page)

  const connectionCard = page.locator('.p-card', { hasText: 'Blink Connection' })
  const retrying = connectionCard.getByTestId('download-retrying')
  await expect(retrying).toContainText('Retrying downloads')
  await expect(retrying.locator('.val')).toHaveText('3 clips')
  await expect(retrying.locator('.val')).toHaveClass(/warn/)

  const givenUp = connectionCard.getByTestId('download-given-up')
  await expect(givenUp).toContainText('Downloads given up (7 days)')
  await expect(givenUp.locator('.val')).toHaveText('1 clip')
  await expect(givenUp.locator('.val')).toHaveClass(/err/)
  // The hover hint says where to find which clips they were.
  await expect(givenUp).toHaveAttribute('title', /add-on log/)
})

test('shows only the retry row when nothing has been given up', async ({ page }) => {
  await page.route('**/api/stats', (route) => withRetries(route, { retrying: 1, given_up: 0 }))

  await openStatus(page)

  const connectionCard = page.locator('.p-card', { hasText: 'Blink Connection' })
  await expect(connectionCard.getByTestId('download-retrying').locator('.val')).toHaveText('1 clip')
  await expect(connectionCard.getByTestId('download-given-up')).toHaveCount(0)
})
