import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// sync-module.spec.ts and sync-module-status.spec.ts both drive this tab
// against a healthy, reachable Sync Module. What neither reaches is the
// tab when Blink is not answering — which is precisely when someone opens
// it, because the cameras have stopped arming and they want to know why.
// A tab that shows an empty, apparently-fine page in that state is worse
// than one that says it could not load.

async function openSyncModule(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')
}

test('a failed load says so rather than showing an empty tab', async ({ page }) => {
  await page.route('**/api/sync-modules', (route) =>
    route.fulfill({ status: 502, contentType: 'application/json', body: '{"error": "not connected"}' }),
  )
  await openSyncModule(page)

  await expect(page.getByText(/Could not load|Failed to load/i).first()).toBeVisible()
  await expect(page.locator('.sync-module-card')).toHaveCount(0)
})

test('an account with no sync modules says so instead of looking broken', async ({ page }) => {
  // A bare array, which is what this endpoint really returns.
  await page.route('**/api/sync-modules', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await openSyncModule(page)

  await expect(page.getByText(/No sync module/i).first()).toBeVisible()
})

test('a module with no USB storage shows no local-clips panel at all', async ({ page }) => {
  // loadLocalStorageClips returns early when nothing reports local_storage,
  // so the panel is absent rather than present-and-empty.
  await page.route('**/api/sync-modules', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          name: 'E2E Module',
          network_id: 1,
          serial: 'E2E-SERIAL',
          version: '2.13.30',
          status: 'online',
          online: true,
          armed: true,
          region_id: 'e2e',
          local_storage: false,
          cameras: [
            {
              name: 'Front Door',
              armed: true,
              online: true,
              battery_state: 'ok',
              battery_level: 90,
              wifi_strength: 3,
            },
          ],
        },
      ]),
    }),
  )
  await openSyncModule(page)

  await expect(page.getByText('E2E Module')).toBeVisible()
  await expect(page.getByText('Local Storage Clips')).toHaveCount(0)
})
