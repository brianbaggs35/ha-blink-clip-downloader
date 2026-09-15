import { test, expect } from './coverage-fixtures'

// The clip modal's AI panel is where 6.0.0's deterministic security layer
// meets a single clip: what the detector observed, how good that evidence
// was, and -- when the risk threshold fired the alert rather than the model
// -- that the model itself said nothing. Producing real security events
// here would need a running YOLO, so the AI result endpoint is served
// directly, the same "mock the API layer, not the application" approach
// live-view and mocked-integrations use.

const CLIP = 'e2e-clip-004'

function event(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    clip_id: CLIP,
    camera: 'Front Door',
    event_type: 'subject_present',
    severity: 'routine',
    confidence: 0.8,
    risk_score: 0,
    evidence_quality: 0.9,
    detail: 'A person was visible for at least 14s.',
    subject_label: 'person',
    track_id: 1,
    asset_name: '',
    asset_type: '',
    start_offset: 0,
    end_offset: 14,
    evidence: {},
    ...over,
  }
}

async function serveAiResult(page: import('@playwright/test').Page, result: Record<string, unknown>) {
  await page.route(`**/api/ai/results/${CLIP}`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result) })
  })
}

const BASE = {
  clip_id: CLIP,
  camera: 'Front Door',
  model: 'llava',
  is_suspicious: true,
  confidence: 0.82,
  summary: 'A person is standing at the driver door of the vehicle.',
  frame_count: 5,
  analyzed_at: '2026-01-05T10:00:00Z',
  response_text: '{"suspicious": true}',
}

async function openPanel(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  await page.locator(`.clip-card[data-id="${CLIP}"]`).click()
  const modal = page.locator('.modal-bg.open')
  await modal.locator('.ai-panel-hdr').click()
  return modal
}

test('the security assessment leads with the most severe thing that happened', async ({ page }) => {
  // Four events in deliberately unhelpful order: a list led by "a person
  // was visible" buries the reason the clip matters at all.
  await serveAiResult(page, {
    ...BASE,
    severity: 'critical',
    risk_score: 81.4,
    evidence_quality: 0.92,
    security_events: [
      event(),
      event({
        id: 2,
        event_type: 'zone_entered',
        severity: 'noteworthy',
        start_offset: 4,
        detail: 'Entered the marked area.',
      }),
      event({
        id: 3,
        event_type: 'impact_candidate',
        severity: 'critical',
        start_offset: 9,
        detail: 'Possible impact with the Silver Kia.',
      }),
      event({
        id: 4,
        event_type: 'contact_candidate',
        severity: 'suspicious',
        start_offset: 7,
        detail: 'Possible contact with the Silver Kia.',
      }),
    ],
  })

  const modal = await openPanel(page)
  const panel = modal.locator('[data-testid="ai-security"]')
  await expect(panel).toBeVisible()
  await expect(panel.getByText('Critical · risk 81')).toBeVisible()
  await expect(panel.getByText('Evidence 92% (strong)')).toBeVisible()

  const rows = panel.locator('.ai-security-list li')
  await expect(rows).toHaveCount(4)
  await expect(rows.nth(0)).toContainText('Impact candidate')
  await expect(rows.nth(0)).toContainText('0:09')
  await expect(rows.nth(1)).toContainText('Contact candidate')
  await expect(rows.nth(2)).toContainText('Zone entered')
  await expect(rows.nth(3)).toContainText('Subject present')
})

test('a clip flagged by the risk threshold says the model itself found nothing', async ({ page }) => {
  // Without this line the panel shows a Suspicious badge next to a summary
  // that reads as unremarkable, with nothing explaining the contradiction.
  await serveAiResult(page, {
    ...BASE,
    summary: 'A person walks across the driveway.',
    severity: 'suspicious',
    risk_score: 78,
    evidence_quality: 0.55,
    risk_override_applied: true,
    security_events: [event({ severity: 'suspicious', event_type: 'asset_proximity' })],
  })

  const modal = await openPanel(page)
  const panel = modal.locator('[data-testid="ai-security"]')
  await expect(
    panel.getByText('Flagged on detection evidence — the AI model itself reported nothing unusual.'),
  ).toBeVisible()
  await expect(panel.getByText('Evidence 55% (moderate)')).toBeVisible()
})

test('weak evidence is labelled as weak rather than quietly shown as a number', async ({ page }) => {
  await serveAiResult(page, {
    ...BASE,
    severity: 'noteworthy',
    risk_score: 22,
    evidence_quality: 0.21,
    security_events: [event({ severity: 'noteworthy' })],
  })
  const modal = await openPanel(page)
  await expect(modal.locator('[data-testid="ai-security"]').getByText('Evidence 21% (weak)')).toBeVisible()
})

test('a clip the security layer found nothing in shows no empty assessment block', async ({ page }) => {
  // Anyone with the optional detection pipeline switched off sees this on
  // every single clip, so an always-present empty section would be noise.
  await serveAiResult(page, { ...BASE, security_events: [] })
  const modal = await openPanel(page)
  await expect(modal.getByText('A person is standing at the driver door of the vehicle.')).toBeVisible()
  await expect(modal.locator('[data-testid="ai-security"]')).toHaveCount(0)
})
