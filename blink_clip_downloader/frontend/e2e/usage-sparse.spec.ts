import { test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// ai-usage-breakdown.spec.ts drives this tab with a paid, token-metered
// provider that has real spend recorded. That leaves the other arm of
// nearly every condition on the page untested, and those arms are not
// edge cases — they are what a local provider looks like (no tokens, no
// cost) and what any provider looks like before the first analysis has
// run. A tab that invents a cost figure, or divides by zero clips, would
// only misbehave in exactly those states.

function usage(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'openai',
    model: 'gpt-4o-mini',
    total_analyses: 40,
    total_tokens_prompt: 120000,
    total_tokens_completion: 8000,
    total_tokens: 128000,
    total_escalations: 0,
    total_escalation_tokens: 0,
    by_model: [
      {
        model: 'gpt-4o-mini',
        provider: 'openai',
        analyses: 40,
        tokens_prompt: 120000,
        tokens_completion: 8000,
        escalated: false,
        cost: 0.0228,
      },
    ],
    total_estimated_cost: 0.0228,
    daily: [
      {
        day: '2026-01-04',
        analyses: 18,
        tokens_prompt: 54000,
        tokens_completion: 3600,
        tokens_total: 57600,
        cost: 0.0102,
      },
    ],
    ...over,
  }
}

async function openUsage(page: Page, payload: Record<string, unknown>) {
  await page.route('**/api/ai/usage', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) }),
  )
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')
}

test('an analyzer that is on but has never run says so instead of showing zeroes', async ({ page }) => {
  await openUsage(
    page,
    usage({ total_analyses: 0, total_tokens: 0, by_model: [], total_estimated_cost: null, daily: [] }),
  )

  await expect(page.getByText('No analysis data yet. Run the AI analysis to see usage statistics.')).toBeVisible()
  // No cost is known, so no cost tile and no per-clip average — dividing
  // a null cost by zero clips is exactly what must not be rendered.
  await expect(page.locator('.usage-grid .lbl').filter({ hasText: 'Avg. Cost / Clip' })).toHaveCount(0)
  await expect(page.locator('.usage-grid .lbl').filter({ hasText: 'Models Used' })).toHaveCount(0)
  await expect(page.locator('.usage-grid')).not.toContainText('NaN')
  await expect(page.locator('.usage-grid')).not.toContainText('undefined')
  // With no daily rows there is no totals footer to add up.
  await expect(page.locator('tfoot')).toHaveCount(0)
})

test('a provider that is not token-metered shows N/A rather than fabricated token counts', async ({ page }) => {
  // moondream_local is outside TOKEN_PROVIDERS: it runs on-device, so
  // there are no tokens to report even though analyses happened.
  await openUsage(
    page,
    usage({
      provider: 'moondream_local',
      model: 'moondream-0.5b',
      total_estimated_cost: null,
      by_model: [
        {
          model: 'moondream-0.5b',
          provider: 'moondream_local',
          analyses: 12,
          tokens_prompt: 0,
          tokens_completion: 0,
          escalated: false,
          cost: null,
        },
      ],
    }),
  )

  const table = page.locator('table').first()
  await expect(table).toContainText('moondream-0.5b')
  // Four cells: the three token columns, plus the cost column, since
  // fmtCost renders a null cost as N/A too rather than as $0.
  await expect(table.getByText('N/A')).toHaveCount(4)
  // The three token tiles are gone. (The table's column *headers* stay —
  // it is the cells that read N/A — so this is asserted on the tiles.)
  const tileLabels = page.locator('.usage-grid .lbl')
  await expect(tileLabels.filter({ hasText: 'Total Tokens' })).toHaveCount(0)
  await expect(tileLabels.filter({ hasText: 'Prompt Tokens' })).toHaveCount(0)
  await expect(tileLabels.filter({ hasText: 'Completion Tokens' })).toHaveCount(0)
  // One analysed model, so the Models Used tile is still there.
  await expect(tileLabels.filter({ hasText: 'Models Used' })).toHaveCount(1)
})

test('escalation tiles appear only once some escalation has actually happened', async ({ page }) => {
  await openUsage(page, usage())
  // The seeded payload has no escalations, so neither escalation tile is
  // rendered — the pair only makes sense once tier 2 has been used.
  await expect(page.locator('.usage-grid .lbl').filter({ hasText: 'Escalations' })).toHaveCount(0)

  await openUsage(page, usage({ total_escalations: 4, total_escalation_tokens: 9000 }))
  await expect(page.locator('.usage-grid .lbl').filter({ hasText: 'Escalations' })).toHaveCount(1)
  await expect(page.locator('.usage-grid .lbl').filter({ hasText: 'Escalation Tokens' })).toHaveCount(1)
})
