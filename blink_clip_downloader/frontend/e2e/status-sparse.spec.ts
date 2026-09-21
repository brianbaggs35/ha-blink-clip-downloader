import { test, expect } from './coverage-fixtures'
import type { Page, Route } from '@playwright/test'

// status.spec.ts drives this tab with everything reported: a disk card, a
// quota, frame stats, cameras, batteries, a configured analyzer. That is
// the tab on a settled install, and it leaves the other side of every one
// of those conditions untested -- which is the state a *fresh* install is
// actually in, before the first poll cycle has filled anything in.
//
// A card that renders "undefined" or throws on a missing field would only
// ever be seen by someone on their first run, which is the worst possible
// audience for it.

async function fulfilJson(route: Route, json: unknown) {
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) })
}

async function openStatus(page: Page) {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="status"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="status"]')
  await expect(page.locator('#status-page-content')).toBeVisible()
}

test('a first run with nothing recorded yet renders every card it can and omits the rest', async ({ page }) => {
  // Deliberately minimal: no account id, no last download, no disk block,
  // no cameras, no batteries, analyzer switched off.
  await page.route('**/api/stats', (route) => fulfilJson(route, { connected: false, total_count: 0 }))
  await page.route('**/api/cameras', (route) => fulfilJson(route, { cameras: [] }))
  await page.route('**/api/battery/status', (route) => fulfilJson(route, { cameras: [] }))
  await page.route('**/api/ai/status', (route) => fulfilJson(route, { enabled: false }))

  await openStatus(page)

  // Scoped: the sidebar carries its own connection badge with the same word.
  await expect(page.locator('#status-page-content').getByText('Disconnected')).toBeVisible()
  // Nothing anywhere should have rendered a literal undefined/NaN.
  await expect(page.locator('#status-page-content')).not.toContainText('undefined')
  await expect(page.locator('#status-page-content')).not.toContainText('NaN')

  // Each card is conditional on the data behind it, so none of them exist.
  // Asserted on the heading rather than on `.p-card` filtered by text: the
  // word "Storage" appears in more than one card, and a text filter would
  // match whichever of them happened to be on screen.
  for (const heading of [/💾 Storage/, /🖼️ Frames Analyzed/, /📷 Cameras/, /🤖 AI Analysis/]) {
    await expect(page.getByRole('heading', { name: heading })).toHaveCount(0)
  }
  await expect(page.locator('.battery-tile')).toHaveCount(0)
})

test('a disk report with no quota configured shows usage without inventing a limit', async ({ page }) => {
  await page.route('**/api/stats', (route) =>
    fulfilJson(route, {
      connected: true,
      total_count: 3,
      account_id: 'e2e-account',
      last_download: '2026-01-01T00:00:00Z',
      disk: {
        used_bytes: 1_000_000_000,
        used_mb: 1000.0,
        free_bytes: 9_000_000_000,
        free_gb: 8.38,
        total_bytes: 10_000_000_000,
        total_gb: 9.31,
      },
    }),
  )

  await openStatus(page)

  await expect(page.getByRole('heading', { name: /💾 Storage/ })).toBeVisible()
  // Rows are asserted directly: only the Storage card has Used/Quota rows,
  // so no card-level locator is needed to disambiguate them.
  await expect(page.locator('.status-row', { hasText: 'Used' })).toContainText('1000')
  // No quota_bytes, so no quota row and no percentage-of-quota bar.
  await expect(page.locator('.status-row', { hasText: 'Quota' })).toHaveCount(0)
  await expect(page.locator('.prog-bar')).toHaveCount(0)
  await expect(page.locator('#status-page-content')).not.toContainText('undefined')

  // The two rows that only exist when the account reports them.
  await expect(page.getByText('e2e-account')).toBeVisible()
})

test('an enabled analyzer with nothing queued or analyzed yet still renders its card', async ({ page }) => {
  await page.route('**/api/ai/status', (route) =>
    fulfilJson(route, { enabled: true, ai_online: false, provider: 'ollama', model: 'llava' }),
  )

  await openStatus(page)

  await expect(page.getByRole('heading', { name: /🤖 AI Analysis/ })).toBeVisible()
  const ai = page.locator('.p-card').filter({ hasText: '🤖 AI Analysis' }).last()
  await expect(ai.getByText('Offline')).toBeVisible()
  // queue.pending, total_analyzed and suspicious_count are all absent, so
  // their rows are omitted rather than rendered as blanks or zeros.
  await expect(ai.locator('.status-row', { hasText: 'Pending' })).toHaveCount(0)
  await expect(ai).not.toContainText('undefined')
})
