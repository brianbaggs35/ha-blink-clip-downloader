import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The Biometrics tab's notice for face-recognition models that could not be
// loaded. The e2e backend's embedder is a stand-in that always loads, so a
// real failure cannot happen here; the spec answers the one list endpoint
// with the real response plus a `models` report, which keeps the rest of the
// page (privacy banner, enrolled people) real — the same "mock the API layer,
// not the application" split the Recognition results spec makes.
//
// Nothing here changes any data.

const FACES = /\/api\/ai\/faces(\?.*)?$/

async function openBiometrics(page: Page, models: Record<string, unknown> | null) {
  await page.route(FACES, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const body = await response.json()
    await route.fulfill({ response, json: { ...body, models } })
  })
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
}

const failed = {
  stage: 'face recognition',
  state: 'failed',
  detail: 'OSError: offline',
  permanent: false,
  retry_in: 120,
}

test('says why recognition is not working, and what that means for clips, when the models failed to load', async ({
  page,
}) => {
  await openBiometrics(page, failed)
  const notice = page.locator('.status-banner', { hasText: 'could not be loaded' })
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('OSError: offline')
  await expect(notice).toContainText('no clip is recognized as a household member')
  await expect(notice).toContainText('tries again in 2 minutes')
})

test('says waiting will not help for a failure that is permanent', async ({ page }) => {
  await openBiometrics(page, {
    ...failed,
    permanent: true,
    retry_in: null,
    detail: 'facenet_pytorch package is not installed',
  })
  const notice = page.locator('.status-banner', { hasText: 'could not be loaded' })
  await expect(notice).toContainText('facenet_pytorch package is not installed')
  await expect(notice).toContainText('Waiting will not fix this')
})

test('shows no notice when the models loaded or nothing has asked for them yet', async ({ page }) => {
  await openBiometrics(page, { ...failed, state: 'ready', detail: '', retry_in: null })
  await expect(page.getByText('Nobody enrolled yet').or(page.locator('.privacy-banner')).first()).toBeVisible()
  await expect(page.getByText('could not be loaded')).toHaveCount(0)
})
