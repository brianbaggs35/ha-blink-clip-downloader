import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The Recognition results panel on the Biometrics tab: what face recognition
// made of the last 30 days of analyzed clips, and why the rest did not count
// as recognized.
//
// One test runs against the real server, which has recorded nothing (the
// seeded analyzer points at an unreachable provider, so no analysis ever
// completes) and so shows the panel's empty state in the position the rest
// are asserted in. The others answer the one endpoint behind the panel
// themselves: the figures are the point, and the server cannot produce them
// without a working AI provider and real faces — the same "mock the API layer,
// not the application" split mocked-integrations.spec.ts makes.
//
// Nothing here changes any data, so this file is safe wherever it runs in the
// declaration order the other Biometrics specs depend on.

const ENDPOINT = '**/api/ai/faces/recognition-stats'
const PANEL = '.recognition-results-card'

function stats(overrides: Record<string, unknown> = {}) {
  return {
    days: 30,
    recognition_enabled: true,
    ignore_unclear_faces: false,
    clips_checked: 80,
    clips_with_faces: 64,
    clips_recognized: 31,
    clips_set_aside: 0,
    faces_seen: 412,
    faces_matched: 301,
    faces_unclear: 84,
    held_back: {
      unfamiliar: 9,
      too_small: 0,
      unclear_ignorable: 24,
      unclear: 0,
      not_approved: 0,
      extra_people: 0,
      unexamined: 0,
    },
    ...overrides,
  }
}

async function openBiometrics(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
}

test('sits right below the face-bypass activity, and says where its figures will come from', async ({ page }) => {
  await openBiometrics(page)
  const panel = page.locator(PANEL)
  await expect(panel.getByText('Nothing to show yet')).toBeVisible()
  await expect(panel.locator('.results-tile')).toHaveCount(0)
  // Directly after the activity card, not merely somewhere further down.
  await expect(page.locator(`.bypass-activity-card + ${PANEL}`)).toHaveCount(1)
})

test('shows how many clips and faces were recognized, and why the others were not', async ({ page }) => {
  await page.route(ENDPOINT, (route) => route.fulfill({ json: stats() }))
  await openBiometrics(page)
  const panel = page.locator(PANEL)

  await expect(panel.locator('.results-tile').nth(0)).toContainText('64')
  await expect(panel.locator('.results-tile').nth(1)).toContainText('31')
  await expect(panel.locator('.results-tile').nth(1)).toContainText('(48%)')
  await expect(panel.locator('.results-tile').nth(2)).toContainText('33')
  await expect(panel.locator('.faces-line')).toHaveText(
    '412 faces seen: 301 matched a household member, 111 matched nobody (84 unclear, 27 clear).',
  )

  await expect(panel.getByRole('heading', { name: "Why 33 clips didn't count as recognized" })).toBeVisible()
  // Only the reasons that happened, each with what to do about it.
  await expect(panel.locator('[data-reason]')).toHaveCount(2)
  await expect(panel.locator('[data-reason="unfamiliar"] .reason-count')).toHaveText('9')
  await expect(panel.locator('[data-reason="unclear_ignorable"]')).toContainText(
    'Turn on Ignore Unclear Faces in the add-on',
  )
  await expect(panel.locator('.set-aside')).toHaveCount(0)
})

test('sends faces too small to identify to the resolution setting, not to enrolling a visitor', async ({ page }) => {
  // At the default 640px most faces are under the 50px recognition needs to
  // judge one; calling them clear faces that matched nobody would send people
  // off to enroll strangers who were really their own household.
  await page.route(ENDPOINT, (route) =>
    route.fulfill({
      json: stats({
        clips_recognized: 4,
        held_back: { ...stats().held_back, unfamiliar: 2, too_small: 58, unclear_ignorable: 0 },
      }),
    }),
  )
  await openBiometrics(page)
  const panel = page.locator(PANEL)

  await expect(panel.locator('[data-reason]')).toHaveCount(2)
  const small = panel.locator('[data-reason="too_small"]')
  await expect(small.locator('.reason-count')).toHaveText('58')
  await expect(small).toContainText('Faces too small to identify')
  await expect(small).toContainText('Raise Face Recognition Resolution')
})

test('credits Ignore Unclear Faces for the clips it helped once it is on', async ({ page }) => {
  await page.route(ENDPOINT, (route) =>
    route.fulfill({
      json: stats({
        ignore_unclear_faces: true,
        clips_recognized: 52,
        clips_set_aside: 21,
        held_back: { unfamiliar: 9, unclear_ignorable: 3, unclear: 0, not_approved: 0, extra_people: 0, unexamined: 0 },
      }),
    }),
  )
  await openBiometrics(page)
  const panel = page.locator(PANEL)

  await expect(panel.locator('.set-aside')).toContainText(
    'Ignore Unclear Faces counted 21 clips as recognized by setting unclear faces aside.',
  )
  // The setting is already on, so it does not ask for it again.
  const leftover = panel.locator('[data-reason="unclear_ignorable"]')
  await expect(leftover).toContainText('Analyzed before Ignore Unclear Faces was turned on')
  await expect(leftover).not.toContainText('Turn on')
})

test('is not shown when recognition is off and nothing was ever recorded', async ({ page }) => {
  await page.route(ENDPOINT, (route) =>
    route.fulfill({ json: stats({ recognition_enabled: false, clips_checked: 0, clips_with_faces: 0 }) }),
  )
  await openBiometrics(page)
  await expect(page.locator('.bypass-activity-card')).toBeVisible()
  await expect(page.locator(PANEL)).toHaveCount(0)
})

test('refreshing the page data brings in the newest figures', async ({ page }) => {
  let calls = 0
  await page.route(ENDPOINT, (route) => {
    calls += 1
    return route.fulfill({ json: stats({ clips_recognized: calls === 1 ? 31 : 44 }) })
  })
  await openBiometrics(page)
  const recognized = page.locator(PANEL).locator('.results-tile').nth(1)
  await expect(recognized).toContainText('31')

  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(recognized).toContainText('44')
})

test('says it could not load, without taking the rest of the tab with it', async ({ page }) => {
  await page.route(ENDPOINT, (route) => route.fulfill({ status: 500, body: 'boom' }))
  await openBiometrics(page)
  await expect(page.locator(PANEL).getByText('Failed to load recognition results.')).toBeVisible()
  await expect(page.locator('.person-card').first()).toBeVisible()
  await expect(page.locator('.bypass-activity-card')).toBeVisible()
})

test('keeps the figures in a row while the card is wide and stacks them once it is narrow', async ({ page }) => {
  await page.route(ENDPOINT, (route) => route.fulfill({ json: stats() }))
  await openBiometrics(page)
  const tiles = page.locator(`${PANEL} .results-tile`)
  const rows = async () =>
    new Set(await tiles.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().top)))).size

  // Three figures side by side...
  await expect.poll(rows).toBe(1)
  // ...until the card is no wider than a phone's. The layout follows the
  // card, not the screen: beside the sidebar a 700px tablet leaves it as
  // narrow as a phone, and a phone is narrower still.
  await page.setViewportSize({ width: 360, height: 800 })
  await expect.poll(rows).toBe(3)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(
    true,
  )
})

test('long figures stay inside the card on the narrowest phone', async ({ page }) => {
  await page.route(ENDPOINT, (route) =>
    route.fulfill({
      json: stats({
        clips_checked: 123456,
        clips_with_faces: 98765,
        clips_recognized: 54321,
        faces_seen: 1234567,
        faces_matched: 987654,
        faces_unclear: 123456,
        held_back: {
          unfamiliar: 12345,
          unclear_ignorable: 23456,
          unclear: 0,
          not_approved: 0,
          extra_people: 0,
          unexamined: 0,
        },
      }),
    }),
  )
  await openBiometrics(page)
  await page.setViewportSize({ width: 320, height: 800 })
  const panel = page.locator(PANEL)
  await expect(panel.locator('[data-reason="unfamiliar"] .reason-count')).toHaveText('12345')
  // Nothing in the card is wider than the box it sits in, and the card is
  // not wider than the screen.
  await expect
    .poll(() =>
      panel.evaluate((card) => {
        const overflowing = [...card.querySelectorAll('*')].filter(
          (el) => getComputedStyle(el).display !== 'inline' && el.scrollWidth > el.clientWidth + 1,
        )
        return overflowing.length + (card.getBoundingClientRect().right > window.innerWidth ? 1 : 0)
      }),
    )
    .toBe(0)
})

test('the set-aside note is legible in both themes', async ({ page }) => {
  await page.route(ENDPOINT, (route) =>
    route.fulfill({ json: stats({ ignore_unclear_faces: true, clips_recognized: 52, clips_set_aside: 21 }) }),
  )
  await openBiometrics(page)
  // PrimeVue's own info text is 3.9:1 on its dark tint, so the note takes the
  // theme's text colour instead (checked with axe; this keeps it that way).
  const note = page.locator(`${PANEL} .set-aside .p-message-text`)
  await expect(note).toHaveCSS('color', 'rgb(242, 242, 247)')
  await page.getByRole('button', { name: 'Switch to light theme' }).click()
  await expect(note).toHaveCSS('color', 'rgb(22, 22, 31)')
})
