import { test, expect } from './coverage-fixtures'

// The Security tab shows two independent judgements side by side: the
// code-computed risk, and the AI model's own verdict on the same clip.
// Where they disagree is exactly the clip a person most needs to look at
// themselves, so the tab has to say so rather than quietly showing the
// higher of the two. Timeline rows are served directly here -- producing
// them for real needs a running YOLO -- following the same approach
// security.spec.ts already uses for its own seeded rows.

function row(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    clip_id: 'verdict-1',
    camera: 'Driveway',
    event_type: 'asset_proximity',
    severity: 'suspicious',
    confidence: 0.75,
    risk_score: 61,
    evidence_quality: 0.8,
    detail: 'The person came within about 1 ft of the Silver Kia.',
    subject_label: 'person',
    track_id: 1,
    asset_name: 'Silver Kia',
    asset_type: 'vehicle',
    start_offset: 6,
    end_offset: 9,
    evidence: {},
    clip_timestamp: '2026-01-05T10:00:00Z',
    file_path: '/share/blink-clips/Driveway/verdict-1.mp4',
    starred: false,
    archived: false,
    ai_suspicious: true,
    ai_confidence: 0.83,
    ai_summary: 'A person is standing at the driver door.',
    risk_override_applied: false,
    ...over,
  }
}

async function showTimeline(page: import('@playwright/test').Page, rows: unknown[]) {
  await page.route('**/api/security/timeline**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ events: rows, total: rows.length }),
    })
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="security"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="security"]')
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('agreement between code and model shows both verdicts and no warning', async ({ page }) => {
  await showTimeline(page, [row()])
  await expect(page.getByText('AI: suspicious')).toBeVisible()
  await expect(page.getByText('Risk 61')).toBeVisible()
  await expect(page.locator('.security-disagree')).toHaveCount(0)
})

test('code concerned and the model unbothered is called out as a disagreement', async ({ page }) => {
  await showTimeline(page, [
    row({
      clip_id: 'verdict-2',
      ai_suspicious: false,
      ai_summary: 'A delivery driver walks to the porch.',
    }),
  ])
  await expect(page.getByText('AI: nothing unusual')).toBeVisible()
  await expect(page.locator('.security-disagree')).toContainText('disagreement')
})

test('the model flagging a clip the geometry rated routine is also a disagreement', async ({ page }) => {
  await showTimeline(page, [
    row({
      clip_id: 'verdict-3',
      severity: 'routine',
      risk_score: 4,
      event_type: 'subject_present',
      ai_suspicious: true,
    }),
  ])
  await expect(page.locator('.security-disagree')).toContainText('disagreement')
})

test('a clip flagged on evidence alone is labelled as such, not as a disagreement', async ({ page }) => {
  // The risk threshold already fired the alert, so there is no unresolved
  // conflict left for a person to arbitrate.
  await showTimeline(page, [row({ clip_id: 'verdict-4', ai_suspicious: false, risk_override_applied: true })])
  await expect(page.getByText('Flagged on evidence')).toBeVisible()
  await expect(page.locator('.security-disagree')).toHaveCount(0)
})

test('a clip with no analysis at all shows the risk alone, claiming no verdict', async ({ page }) => {
  await showTimeline(page, [row({ clip_id: 'verdict-5', ai_suspicious: null, ai_confidence: null, ai_summary: null })])
  await expect(page.getByText('Risk 61')).toBeVisible()
  await expect(page.locator('.security-verdict')).toHaveCount(0)
  await expect(page.locator('.security-disagree')).toHaveCount(0)
})

test('a clip timestamp that cannot be parsed is shown as-is rather than as Invalid Date', async ({ page }) => {
  await showTimeline(page, [row({ clip_id: 'verdict-6', clip_timestamp: 'not-a-timestamp' })])
  await expect(page.locator('.security-when').first()).toHaveText('not-a-timestamp')
  await expect(page.getByText('Invalid Date')).toHaveCount(0)
})
