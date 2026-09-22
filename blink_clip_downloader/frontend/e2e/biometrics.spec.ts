import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// Biometrics runs against the real server and database with one stand-in:
// standalone_server.py's _E2EFaceEmbedder replaces facenet-pytorch's models
// (an optional extra this environment doesn't install) with a detector that
// finds the same three faces in any readable image — two people and one too
// blurred to offer by default. Everything else is the real thing: ffmpeg
// extracting frames from a real clip, collapsing the identical shots,
// grouping, holding candidates server-side, enrolling from them, storing and
// serving the thumbnail, and recognizing the enrolled face on the next scan.
//
// Declaration order is execution order (workers: 1), and later tests build
// on "Morgan E2E", whom the first flow enrolls. Alex and Jordan must survive
// this file: library-modal.spec.ts's face-report picker relies on them.

// A real 32x32 JPEG (Pillow-generated). The "1x1 JPEG" the old picker's
// test used never decoded — it passed only because that test expected every
// photo to fail.
const PHOTO_JPEG = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAAgACADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDdooorzD2QooooAKKKKACiiigD/9k=',
  'base64',
)

async function openBiometrics(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="biometrics"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="biometrics"]')
  await expect(page.locator('.person-card').first()).toBeVisible()
}

function personCard(page: Page, name: string) {
  return page.locator('.person-card').filter({ has: page.locator('.person-name', { hasText: name }) })
}

/** A card located by position, which survives its name turning into an input. */
async function cardAt(page: Page, name: string) {
  const names = await page.locator('.person-card .person-name').allTextContents()
  const index = names.indexOf(name)
  expect(index).toBeGreaterThanOrEqual(0)
  return page.locator('.person-card').nth(index)
}

async function chooseCamera(page: Page, camera: string) {
  await page.locator('#biometrics-camera-select').click()
  await page.getByRole('option', { name: camera, exact: true }).click()
}

async function uploadPhoto(page: Page) {
  await page.getByRole('tab', { name: 'From a photo' }).click()
  await page.locator('input[type="file"]').setInputFiles({ name: 'me.jpg', mimeType: 'image/jpeg', buffer: PHOTO_JPEG })
  await expect(page.locator('.photo-results').getByText('3 faces found')).toBeVisible()
}

test.beforeEach(async ({ page }) => {
  await openBiometrics(page)
})

test('lists enrolled people with their approval, photo counts and anything to review', async ({ page }) => {
  const alex = personCard(page, 'Alex E2E')
  await expect(alex.getByText('Partially approved')).toBeVisible()
  await expect(alex.getByText('2 photos')).toBeVisible()
  // Seeded as if by an earlier version: no stored image, so initials, and a
  // nudge to add fresh photos rather than a warning.
  await expect(alex.getByText('Enrolled with an earlier version')).toBeVisible()
  await expect(alex.getByText(/to review/)).toHaveCount(0)

  const jordan = personCard(page, 'Jordan E2E')
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()
  await expect(jordan.getByText('1 photo', { exact: true })).toBeVisible()

  await expect(personCard(page, 'Riley E2E').getByText('1 to review')).toBeVisible()
  await expect(page.getByText(/of \d+ enrolled (person is|people are) approved/)).toBeVisible()
})

test("the photos dialog explains a photo that doesn't look like the rest", async ({ page }) => {
  await personCard(page, 'Riley E2E').getByRole('button', { name: 'Photos', exact: true }).click()
  const dialog = page.locator('.person-photos-dialog')
  await expect(dialog.getByText("Riley E2E's photos")).toBeVisible()
  await expect(dialog.locator('.photo-item')).toHaveCount(3)
  await expect(dialog.locator('.photo-item.flagged')).toHaveCount(1)
  await expect(dialog.getByText("Doesn't look like this person's other photos")).toBeVisible()
  await dialog.locator('.p-dialog-footer').getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toBeHidden()
})

test('the camera picker lists every camera, and finds faces in the clip picked', async ({ page }) => {
  await page.locator('#biometrics-camera-select').click()
  for (const camera of ['All cameras', 'Test Scratch', 'Front Door', 'Backyard', 'Garage']) {
    await expect(page.getByRole('option', { name: camera, exact: true })).toBeVisible()
  }
  await page.getByRole('option', { name: 'Test Scratch', exact: true }).click()

  // The seeded biometrics-source clip is the only Test Scratch clip in the
  // default 24 hours.
  const tile = page.locator('.clip-tile')
  await expect(tile).toHaveCount(1)
  await tile.click()
  await expect(tile).toContainText('3 faces')

  // Six frames, three faces each: the identical shots collapse to one per
  // face, and the blurred one waits behind a toggle.
  const groups = page.locator('.face-group')
  await expect(groups).toHaveCount(2)
  await expect(groups.first()).toContainText('Person 1')
  await expect(groups.first().locator('.face-tile')).toContainText('Good')
  await page.getByText('Show 1 low-quality face').click()
  await expect(groups).toHaveCount(3)
})

test('enrolls a new person from faces found in a clip', async ({ page }) => {
  await chooseCamera(page, 'Test Scratch')
  await page.locator('.clip-tile').click()
  const firstGroup = page.locator('.face-group').first()
  await firstGroup.getByRole('button', { name: 'Select all' }).click()

  const bar = page.locator('.enroll-bar')
  await expect(bar).toContainText('1 face selected')
  await page.locator('#biometrics-name').fill('Morgan E2E')
  await expect(page.locator('#biometrics-approve-new')).toBeVisible()
  await bar.getByRole('button', { name: 'Enroll as Morgan E2E' }).click()

  await expect(page.getByText('Enrolled 1 photo of Morgan E2E')).toBeVisible()
  await expect(bar).toBeHidden()
  const morgan = personCard(page, 'Morgan E2E')
  await expect(morgan.getByText('Approved', { exact: true })).toBeVisible()
  // The face is stored and served back — a real image, not initials.
  const avatar = morgan.locator('.p-avatar img').first()
  await expect(avatar).toHaveAttribute('src', /\/api\/ai\/faces\/thumbs\/\d+$/)
  await expect.poll(() => avatar.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0)
})

test('recognizes someone already enrolled, and adds more photos to them in one step', async ({ page }) => {
  await uploadPhoto(page)
  const recognized = page.locator('.face-group', { hasText: 'Already recognized as Morgan E2E' })
  await expect(recognized).toBeVisible()
  await recognized.getByRole('button', { name: 'Add to Morgan E2E' }).click()

  const bar = page.locator('.enroll-bar')
  await expect(bar).toContainText('Adds to Morgan E2E, who stays approved')
  await expect(page.locator('#biometrics-approve-new')).toHaveCount(0)
  await bar.getByRole('button', { name: 'Enroll as Morgan E2E' }).click()
  await expect(page.getByText('Enrolled 1 photo of Morgan E2E')).toBeVisible()
  await expect(personCard(page, 'Morgan E2E').getByText('2 photos')).toBeVisible()
})

test('warns before filing a recognized face under someone else', async ({ page }) => {
  await uploadPhoto(page)
  await page.locator('.face-group', { hasText: 'Already recognized as Morgan E2E' }).locator('.face-tile').click()
  await page.locator('#biometrics-name').fill('Jordan E2E')
  const bar = page.locator('.enroll-bar')
  await expect(bar).toContainText('already recognized as Morgan E2E')
  await expect(bar).toContainText('Adds to Jordan E2E')
  await bar.getByRole('button', { name: 'Clear selection' }).click()
  await expect(bar).toBeHidden()
})

test('removes one photo of a person, keeping the rest', async ({ page }) => {
  const morgan = personCard(page, 'Morgan E2E')
  await morgan.getByRole('button', { name: 'Photos', exact: true }).click()
  const dialog = page.locator('.person-photos-dialog')
  await expect(dialog.locator('.photo-item')).toHaveCount(2)
  await dialog.getByRole('button', { name: 'Remove photo' }).first().click()
  await expect(page.getByText('Remove this photo of Morgan E2E?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText('Photo removed')).toBeVisible()
  await expect(dialog.locator('.photo-item')).toHaveCount(1)
  await expect(morgan.getByText('1 photo', { exact: true })).toBeVisible()
})

test("toggling a person's approval switch flips their badge", async ({ page }) => {
  const jordan = personCard(page, 'Jordan E2E')
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()

  await jordan.locator('input[role="switch"]').click()
  await expect(jordan.getByText('Not approved')).toBeVisible()
  await expect(page.getByText('Jordan E2E no longer clears alerts')).toBeVisible()

  await jordan.locator('input[role="switch"]').click()
  await expect(jordan.getByText('Approved', { exact: true })).toBeVisible()
  await expect(page.getByText('Jordan E2E can now clear alerts')).toBeVisible()
})

test('renaming then removing a person updates and clears their card', async ({ page }) => {
  const casey = await cardAt(page, 'Casey E2E')
  await casey.getByRole('button', { name: 'Rename' }).click()
  await casey.locator('.rename-input').fill('Casey Renamed E2E')
  await casey.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Renamed to Casey Renamed E2E')).toBeVisible()

  const renamed = personCard(page, 'Casey Renamed E2E')
  await expect(renamed).toBeVisible()
  await expect(personCard(page, 'Casey E2E')).toHaveCount(0)

  await renamed.getByRole('button', { name: 'Remove' }).click()
  await expect(page.getByText('Remove "Casey Renamed E2E" (1 photo) from Biometrics?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText('Removed Casey Renamed E2E')).toBeVisible()
  await expect(personCard(page, 'Casey Renamed E2E')).toHaveCount(0)
})

test('renaming can be cancelled, and an empty name is rejected', async ({ page }) => {
  const jordan = await cardAt(page, 'Jordan E2E')
  await jordan.getByRole('button', { name: 'Rename' }).click()
  const input = jordan.locator('.rename-input')
  await input.fill('Should not save')
  await jordan.getByRole('button', { name: 'Cancel' }).click()
  await expect(input).toHaveCount(0)
  await expect(jordan).toContainText('Jordan E2E')

  await jordan.getByRole('button', { name: 'Rename' }).click()
  await jordan.locator('.rename-input').fill('   ')
  await jordan.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Name cannot be empty')).toBeVisible()
})

test('a camera renamed or removed while selected falls back to all cameras', async ({ page }) => {
  await chooseCamera(page, 'Test Scratch')
  await expect(page.locator('.clip-tile')).toHaveCount(1)

  // The next camera list no longer has Test Scratch — what a rename carried
  // across the library, or a camera removed from the account, looks like.
  await page.route('**/api/cameras', async (route) => {
    const response = await route.fetch()
    const cameras = (await response.json()) as { camera: string }[]
    await route.fulfill({ response, json: cameras.filter((c) => c.camera !== 'Test Scratch') })
  })
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()

  await expect(page.locator('#biometrics-camera-select')).toHaveText('All cameras')
  await expect(page.locator('.clip-tile').first()).toBeVisible()
  await page.locator('#biometrics-camera-select').click()
  await expect(page.getByRole('option', { name: 'Test Scratch', exact: true })).toHaveCount(0)
})

test('a missed-match report opens a scan of that clip', async ({ page }) => {
  await page.route('**/api/ai/faces/feedback', (route) =>
    route.fulfill({
      json: [
        {
          clip_id: 'e2e-biometrics-source',
          camera: 'Test Scratch',
          report_type: 'false_negative',
          note: '',
          person_name: 'Alex E2E',
          created_at: '2026-01-01T13:00:00Z',
        },
      ],
    }),
  )
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await page.getByRole('button', { name: 'Find faces in this clip' }).click()
  await expect(page.locator('.face-group').first()).toBeVisible()
  await expect(page.getByText('Already recognized as Morgan E2E')).toBeVisible()
})
