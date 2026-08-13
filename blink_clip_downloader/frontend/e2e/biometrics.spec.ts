import { test, expect } from './coverage-fixtures'

// Biometrics self-hides from the nav (AppSidebar.vue) whenever GET
// /api/ai/faces reports available=false, which vision.py's real
// is_face_recognition_available() genuinely does in this lightweight test
// environment (facenet_pytorch is an optional CV-pipeline extra, not
// installed here). standalone_server.py patches that one check to True so
// this tab — and its person-management CRUD, none of which needs the real
// ML pipeline — is e2e-reachable; the actual embedding step still
// independently (and gracefully) reports no face detected either way, so
// an enrollment can never actually succeed here, real or otherwise.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
})

test('renders enrolled people with mixed-approval and single-approval badges', async ({ page }) => {
  const alex = page.locator('.person-card', { hasText: 'Alex E2E' })
  await expect(alex.getByText('Partially approved')).toBeVisible()
  await expect(alex.getByText('2 photos')).toBeVisible()

  const jordan = page.locator('.person-card', { hasText: 'Jordan E2E' })
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()
  await expect(jordan.getByText('1 photo', { exact: true })).toBeVisible()

  await expect(page.getByText(/of \d+ enrolled (person is|people are) approved/)).toBeVisible()
})

test('the mode toggle switches between the clip picker and the photo upload UI', async ({ page }) => {
  await expect(page.locator('#biometrics-camera-select')).toBeVisible()
  await expect(page.locator('.photo-picker')).toHaveCount(0)

  await page.getByRole('button', { name: 'Upload a photo' }).click()
  await expect(page.locator('.photo-picker')).toBeVisible()
  await expect(page.locator('#biometrics-camera-select')).toHaveCount(0)

  await page.getByRole('button', { name: 'From a clip' }).click()
  await expect(page.locator('#biometrics-camera-select')).toBeVisible()
})

test('selecting an existing person from the dropdown enables Add to person', async ({ page }) => {
  const addBtn = page.getByRole('button', { name: '➕ Add to person' })
  await expect(addBtn).toBeDisabled()

  await page.locator('#biometrics-add-to-existing').click()
  await page.getByRole('option', { name: 'Jordan E2E' }).click()
  await expect(addBtn).toBeEnabled()
})

test('enrolling from a real extracted frame gracefully reports no face detected', async ({ page }) => {
  // The seeded biometrics-source clip is a real, ffmpeg-generated video (not
  // just placeholder bytes — see standalone_server.py), so
  // GET /api/clips/{id}/frames genuinely extracts real JPEG frames here
  // rather than failing extraction outright. FaceEmbedder still
  // independently discovers facenet_pytorch is missing during the actual
  // embed step and returns no detected face — a real, already-handled
  // response (see media_server.py's _handle_faces_enroll), not a mocked one.
  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: 'Test Scratch' }).click()
  await expect(page.locator('.thumb-strip-item')).toHaveCount(1)
  await page.locator('.thumb-strip-item').first().click()

  const frames = page.locator('.frame-item')
  await expect(frames).toHaveCount(3)
  await frames.first().click()
  await expect(frames.first()).toHaveClass(/selected/)

  await page.locator('#biometrics-name').fill('e2e nobody')
  await page.getByRole('button', { name: /Enroll 1 selected frame/ }).click()

  await expect(page.getByText('Enrollment failed for every selected frame — no clear face detected')).toBeVisible()
})

test('narrowing the lookback window filters out the clip and shows the empty state', async ({ page }) => {
  // e2e-biometrics-source sits at hours_ago=12 (standalone_server.py) — well
  // inside the default 24h lookback (see the test above) but outside a 6h
  // one, so switching the selector genuinely re-queries and empties the
  // strip rather than this being a simulated empty state.
  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: 'Test Scratch' }).click()
  await expect(page.locator('.thumb-strip-item')).toHaveCount(1)

  await page.locator('#biometrics-lookback-select').click()
  await page.getByRole('option', { name: 'Last 6 hours' }).click()
  await expect(
    page.getByText('No clips for this camera in that time range — try a longer lookback above.'),
  ).toBeVisible()
  await expect(page.locator('.thumb-strip-item')).toHaveCount(0)
})

// Mutates Jordan's approval state — kept apart from the read-only
// assertions above (which check Jordan's *original* seeded state) by not
// running until after them. Declaration order is execution order here
// (workers: 1, no intra-file parallelism).
test("toggling a person's approval switch flips their badge", async ({ page }) => {
  const jordan = page.locator('.person-card', { hasText: 'Jordan E2E' })
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()

  await jordan.locator('input[role="switch"]').click()
  await expect(jordan.getByText('Not approved')).toBeVisible()
  await expect(page.getByText('Jordan E2E no longer approved')).toBeVisible()

  await jordan.locator('input[role="switch"]').click()
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()
  await expect(page.getByText('Jordan E2E approved for auto-clear')).toBeVisible()
})

test('renaming then removing a person updates and clears their card', async ({ page }) => {
  // Position-based, not text-based: groupedPeople is sorted by name (Alex,
  // Casey, Jordan), so Casey is reliably index 1 — needed because once
  // editing starts, the name text is replaced by an <input>'s *value*
  // (not part of the card's text content), so a locator re-filtered by
  // "Casey E2E" text stops matching anything the moment rename mode opens.
  const casey = page.locator('.person-card').nth(1)
  await expect(casey).toContainText('Casey E2E')
  // By its stable title attribute, not role/name — the button's only
  // visible content is the "✎" glyph, which is what becomes its
  // accessible name (title is just a hover tooltip here, not included).
  await casey.locator('button[title="Rename"]').click()
  await casey.locator('.rename-input').fill('Casey Renamed E2E')
  await casey.getByRole('button', { name: 'Save' }).click()

  await expect(casey).toContainText('Casey Renamed E2E')
  await expect(page.locator('.person-card', { hasText: 'Casey E2E' })).toHaveCount(0)

  await casey.getByRole('button', { name: 'Remove' }).click()
  await expect(page.getByText('Remove "Casey Renamed E2E" (1 photo) from Biometrics?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('Removed Casey Renamed E2E')).toBeVisible()
  await expect(page.locator('.person-card', { hasText: 'Casey Renamed E2E' })).toHaveCount(0)
})
