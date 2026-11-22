import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// Which face-recognition backend runs (ai_face_recognition_backend), as the
// Biometrics tab shows it. The e2e backend runs the default, FaceNet, so the
// first test reads the real API; the others answer the list endpoint with the
// real response plus the fields an OpenCV server adds, which keeps the rest of
// the page real — the same "mock the API layer, not the application" split
// biometrics-models-failed.spec.ts makes.
//
// Nothing here changes any data.

const FACES = /\/api\/ai\/faces(\?.*)?$/

type Body = Record<string, unknown> & { faces: object[] }

async function openBiometrics(page: Page, change: (body: Body) => Body) {
  await page.route(FACES, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    await route.fulfill({ response, json: change(await response.json()) })
  })
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
}

test('the real server runs FaceNet and counts every seeded photo as in use', async ({ page }) => {
  const body = await (await page.request.get('/api/ai/faces')).json()
  expect(body.backend).toBe('facenet')
  expect(body.backend_measured).toBe(true)
  expect(body.faces.length).toBeGreaterThan(0)
  expect(body.faces.every((f: { other_backend: boolean }) => f.other_backend === false)).toBe(true)
})

test('says nothing about the backend on the default one', async ({ page }) => {
  await openBiometrics(page, (body) => body)
  await expect(page.locator('.privacy-banner')).toBeVisible()
  await expect(page.getByText('experimental')).toHaveCount(0)
  await expect(page.getByText('other face-recognition backend')).toHaveCount(0)
})

test('warns that the OpenCV backend is experimental', async ({ page }) => {
  await openBiometrics(page, (body) => ({ ...body, backend: 'opencv', backend_measured: false }))
  const notice = page.locator('.status-banner', { hasText: 'experimental' })
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('have not been measured')
  await expect(notice).toContainText('never sets an unclear face aside')
})

test('says when photos were made with the other backend and are not used', async ({ page }) => {
  await openBiometrics(page, (body) => ({
    ...body,
    backend: 'opencv',
    backend_measured: false,
    faces: body.faces.map((f: object) => ({ ...f, other_backend: true })),
  }))
  const notice = page.locator('.status-banner', { hasText: 'other face-recognition backend' })
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('enrolled with the other face-recognition backend')
  await expect(notice).toContainText('not recognized')
})
