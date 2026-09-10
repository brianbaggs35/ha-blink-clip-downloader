import { test, expect } from './coverage-fixtures'

// A second sync module snapshot (same shape _FakeSyncModule.snapshot()
// returns, but with local_storage: true and two fake clips) — mocked via
// page.route() rather than seeded into standalone_server.py's shared
// fixture data, since every one of its clips/cameras/total-count
// assertions elsewhere (status.spec.ts in particular) already depends on
// the real seeded data staying exactly as-is. Registered mid-test, after
// beforeEach's own navigation already completed against the real
// backend, then followed by page.reload() so the page's *next* fetch
// actually goes through these routes — the same "mock the API layer, not
// the application" approach mocked-integrations.spec.ts and
// live-view.spec.ts already use for Google Drive/Live View.
const LOCAL_STORAGE_MODULE = {
  name: 'Home',
  network_id: 10,
  serial: 'E2E-SYNC-0001',
  version: '2.13.30',
  status: 'online',
  online: true,
  armed: true,
  region_id: 'e2e',
  local_storage: true,
  cameras: [
    {
      name: 'Front Door',
      armed: true,
      online: true,
      battery_state: 'ok',
      battery_level: 3,
      wifi_strength: -60,
      type: 'catalina',
    },
    {
      name: 'Backyard',
      armed: true,
      online: true,
      battery_state: 'low',
      battery_level: 1,
      wifi_strength: -60,
      type: 'catalina',
    },
    {
      name: 'Garage',
      armed: true,
      online: false,
      battery_state: 'ok',
      battery_level: 3,
      wifi_strength: -60,
      type: 'catalina',
    },
  ],
}

function localStorageClip(id: string, camera: string) {
  return {
    id,
    camera,
    file_path: `/share/blink-clips/${camera}/${id}.mp4`,
    timestamp: '2026-01-05T10:00:00Z',
    size_bytes: 2_000_000,
    duration: 8,
    source: 'local_storage',
    network_id: 10,
    starred: false,
    tags: [],
    downloaded_at: '2026-01-05T10:01:00Z',
    archived: false,
    archive_path: '',
    gdrive_backed_up: false,
    gdrive_file_id: '',
    gdrive_uploaded_at: '',
  }
}

async function mockLocalStorageClips(page: import('@playwright/test').Page) {
  await page.route('**/api/sync-modules', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([LOCAL_STORAGE_MODULE]) }),
  )
  const clips = [
    localStorageClip('e2e-local-storage-1', 'Front Door'),
    localStorageClip('e2e-local-storage-2', 'Backyard'),
  ]
  await page.route('**/api/clips**', async (route) => {
    const url = route.request().url()
    if (url.includes('/api/clips/e2e-local-storage-1')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(clips[0]) })
    }
    if (url.includes('source=local_storage')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(clips) })
    }
    return route.continue()
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
}

// scripts/standalone_server.py's _FakeSyncModule: one sync module ("Home",
// serial E2E-SYNC-0001, firmware 2.13.30) with the same three cameras every
// other tab's fake data uses (Front Door, Backyard, Garage) -- Garage
// starts offline and Backyard starts with a low battery, everything else
// starts armed, matching this suite's general "seed a few interesting
// states rather than all-identical fixtures" convention.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
})

test('shows the sync module info and every one of its cameras', async ({ page }) => {
  // Library's own nav (always mounted, never v-if-gated) also lists every
  // camera name in its sidebar filter regardless of which tab is active, so
  // scope every assertion here to this tab's own page container.
  const tab = page.locator('#page-syncmodule')
  await expect(tab.getByText('Home', { exact: true })).toBeVisible()
  await expect(tab.getByText('Firmware 2.13.30')).toBeVisible()
  await expect(tab.getByText('Serial E2E-SYNC-0001')).toBeVisible()
  for (const camera of ['Front Door', 'Backyard', 'Garage']) {
    await expect(tab.getByText(camera, { exact: true })).toBeVisible()
  }
  await expect(tab.getByText('Low battery')).toBeVisible()
  const garageCard = tab.locator('.sm-cam-card', { hasText: 'Garage' })
  await expect(garageCard.getByText('Offline', { exact: true })).toBeVisible()
})

test('starts with the system fully armed', async ({ page }) => {
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-armed/)
})

test('toggling one camera off switches the system to partially armed, and re-arming it restores fully armed', async ({
  page,
}) => {
  const frontDoorCard = page.locator('.sm-cam-card', { hasText: 'Front Door' })
  await frontDoorCard.locator('input[role="switch"]').click()
  await expect(frontDoorCard.getByText('Disarmed', { exact: true })).toBeVisible()
  await expect(page.getByText('Front Door disarmed')).toBeVisible()

  // The sync module itself is still armed, but with one of its cameras now
  // disarmed the system as a whole is no longer fully protected -- a real,
  // meaningful difference the headline status must surface.
  await expect(page.locator('.system-hero-title')).toHaveText('Partially Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-mixed/)
  await expect(page.getByText('2 of 3 cameras armed')).toBeVisible()

  // Re-arming the camera brings every camera back to armed, so the system
  // as a whole must go back to fully "System Armed" too.
  await frontDoorCard.locator('input[role="switch"]').click()
  await expect(frontDoorCard.getByText('Armed', { exact: true })).toBeVisible()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.locator('.system-hero')).toHaveClass(/system-hero-armed/)
})

test('disarming the entire system requires confirmation, and does nothing if declined', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})

test('pressing Escape on the disarm confirmation also declines, same as Cancel', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Confirm' })).toHaveCount(0)
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})

test('disarming and re-arming the entire system via the hero button', async ({ page }) => {
  await page.getByRole('button', { name: 'Disarm Entire System' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('Disarmed')
  await expect(page.getByText('Entire system disarmed')).toBeVisible()
  await expect(page.locator('.sync-module-card').getByText('Disarmed', { exact: true }).first()).toBeVisible()

  // Re-arming needs no confirmation.
  await page.getByRole('button', { name: 'Arm Entire System' }).click()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
  await expect(page.getByText('Entire system armed')).toBeVisible()
})

test("toggling the sync module's own switch also asks for confirmation before disarming", async ({ page }) => {
  const homeCard = page.locator('.sync-module-card', { hasText: 'Home' })
  await homeCard.locator('.sm-module-arm input[role="switch"]').click()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.locator('.system-hero-title')).toHaveText('Disarmed')

  // Leave it armed again for any later test/run against this same backend.
  await homeCard.locator('.sm-module-arm input[role="switch"]').click()
  await expect(page.locator('.system-hero-title')).toHaveText('System Armed')
})

test('the real seeded module has no local-storage clips panel at all', async ({ page }) => {
  // Sanity check against the *real*, unmocked backend data — confirms the
  // panel is genuinely conditional on local_storage, not just hidden by
  // some other means, before the mocked tests below exercise the "on" state.
  const homeCard = page.locator('.sync-module-card', { hasText: 'Home' })
  await expect(homeCard.getByText('Local Storage active')).toHaveCount(0)
  await expect(homeCard.getByText(/Local Storage Clips/)).toHaveCount(0)
})

test('shows a collapsed Local Storage Clips panel, and expanding it lists the clips', async ({ page }) => {
  await mockLocalStorageClips(page)

  const homeCard = page.locator('.sync-module-card', { hasText: 'Home' })
  await expect(homeCard.getByText('Local Storage active')).toBeVisible()
  await expect(homeCard.getByText('Local Storage Clips (2)')).toBeVisible()
  // PrimeVue's Panel keeps collapsed content in the DOM (v-show, not v-if)
  // -- toBeHidden(), not toHaveCount(0), is the correct check here.
  await expect(homeCard.locator('.clip-card').first()).toBeHidden()

  // The toggle is a real <button> (aria-label matches the header text),
  // not just the header's plain text -- click the button specifically so
  // this can't silently pass by clicking inert text instead.
  await homeCard.getByRole('button', { name: 'Local Storage Clips (2)' }).click()
  const clipCards = homeCard.locator('.clip-card')
  await expect(clipCards).toHaveCount(2)
  await expect(clipCards.filter({ hasText: 'Front Door' })).toBeVisible()
  await expect(clipCards.filter({ hasText: 'Backyard' })).toBeVisible()
  // Read-only in this context - no bulk-selection checkbox to confuse with
  // the Library tab's own multi-select flow.
  await expect(clipCards.first().locator('.sel-check')).toHaveCount(0)
})

test("clicking a local-storage clip opens it in the Library tab's modal, without switching tabs", async ({ page }) => {
  await mockLocalStorageClips(page)

  const homeCard = page.locator('.sync-module-card', { hasText: 'Home' })
  await homeCard.getByRole('button', { name: 'Local Storage Clips (2)' }).click()
  await homeCard.locator('.clip-card', { hasText: 'Front Door' }).click()

  const modal = page.locator('.modal-bg.open')
  await expect(modal).toBeVisible()
  // Still visually on the Sync Module tab - the modal is a Teleport-ed
  // overlay from the always-mounted Library page, not a tab switch (see
  // stores/clipViewer.ts).
  await expect(page.locator('.app-nav-tab.active[data-tab="syncmodule"]')).toBeVisible()

  await modal.locator('.modal-close').click()
  await expect(modal).not.toBeVisible()
})
