import { test, expect } from './coverage-fixtures'

// The AI Usage tab is what stops a cloud provider quietly running up a
// bill: tokens, cost per clip, which model spent them, and how much of it
// was tier-2 escalation rather than the primary model. Real usage builds
// up only from real analyses against a reachable provider, so the usage
// payload is served directly here -- the same approach the rest of this
// suite uses for states a throwaway backend cannot reach.

function usage(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'openai',
    model: 'gpt-4o-mini',
    cost_per_1m_input: 0.15,
    cost_per_1m_output: 0.6,
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
      {
        day: '2026-01-05',
        analyses: 22,
        tokens_prompt: 66000,
        tokens_completion: 4400,
        tokens_total: 70400,
        cost: 0.0126,
      },
    ],
    ...over,
  }
}

async function showUsage(page: import('@playwright/test').Page, payload: Record<string, unknown>) {
  await page.route('**/api/ai/usage', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) })
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')
}

function periods(over: Record<string, unknown> = {}) {
  return {
    weekly: [
      {
        period: '2026-W03',
        analyses: 25,
        tokens_prompt: 75000,
        tokens_completion: 5000,
        tokens_total: 80000,
        cost: 0.0143,
      },
      {
        period: '2026-W02',
        analyses: 15,
        tokens_prompt: 45000,
        tokens_completion: 3000,
        tokens_total: 48000,
        cost: 0.0085,
      },
    ],
    monthly: [
      {
        period: '2026-01',
        analyses: 40,
        tokens_prompt: 120000,
        tokens_completion: 8000,
        tokens_total: 128000,
        cost: 0.0228,
      },
      {
        period: '2025-12',
        analyses: 12,
        tokens_prompt: 36000,
        tokens_completion: 2400,
        tokens_total: 38400,
        cost: 0.0068,
      },
    ],
    ...over,
  }
}

/** Serve both usage endpoints and report how many times the rollup one was
 *  actually hit — the lazy-fetch behaviour is the point, so the count is part
 *  of what these tests assert. */
async function showUsageWithPeriods(
  page: import('@playwright/test').Page,
  payload: Record<string, unknown> = usage(),
  periodsPayload: Record<string, unknown> | null = periods(),
) {
  const hits = { periods: 0 }
  await page.route('**/api/ai/usage/periods', async (route) => {
    hits.periods += 1
    if (periodsPayload === null) {
      return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"nope"}' })
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(periodsPayload) })
  })
  await showUsage(page, payload)
  return hits
}

/** The Day/Week/Month segmented control. */
function granularity(page: import('@playwright/test').Page) {
  return page.locator('#page-usage .usage-history-head').getByRole('button')
}

function historyRows(page: import('@playwright/test').Page) {
  return page.locator('#page-usage .usage-history-table tbody tr')
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
})

test('a paid provider shows tokens, total cost and what each clip costs on average', async ({ page }) => {
  await showUsage(page, usage())
  const tab = page.locator('#page-usage')
  await expect(tab).toContainText('Clips Analyzed')
  await expect(tab).toContainText('40')
  // The number that actually tells someone whether leaving this switched on
  // is affordable: $0.0228 spread over those 40 clips.
  await expect(tab).toContainText('$')
  await expect(tab).toContainText('gpt-4o-mini')
})

test('escalation spend is broken out rather than folded into the primary model', async ({ page }) => {
  // Tier 2 can be an entirely different (and dearer) provider, so a single
  // blended number would hide where the money went.
  await showUsage(
    page,
    usage({
      total_escalations: 7,
      total_escalation_tokens: 21000,
      by_model: [
        ...usage().by_model,
        {
          model: 'gpt-4o',
          provider: 'openai',
          analyses: 7,
          tokens_prompt: 19000,
          tokens_completion: 2000,
          escalated: true,
          cost: 0.0672,
        },
      ],
    }),
  )
  await expect(page.locator('#page-usage')).toContainText('gpt-4o')
  await expect(page.locator('#page-usage')).toContainText('gpt-4o-mini')
})

test('a local provider that costs nothing shows no invented cost figure', async ({ page }) => {
  await showUsage(
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
      total_tokens: 0,
      total_tokens_prompt: 0,
      total_tokens_completion: 0,
    }),
  )
  await expect(page.locator('#page-usage')).toContainText('moondream-0.5b')
  await expect(page.locator('#page-usage')).not.toContainText('$0.00')
})

test('AI analysis switched off with nothing recorded says so instead of showing empty tiles', async ({ page }) => {
  await showUsage(
    page,
    usage({
      enabled: false,
      total_analyses: 0,
      total_tokens: 0,
      total_tokens_prompt: 0,
      total_tokens_completion: 0,
      total_estimated_cost: null,
      by_model: [],
      daily: [],
    }),
  )
  await expect(page.locator('#page-usage')).toContainText('No AI Usage Data')
  await expect(page.locator('#page-usage')).toContainText('Enable AI analysis in the add-on settings')
})

test('a malformed usage payload renders the empty state rather than throwing', async ({ page }) => {
  // by_model/daily are always arrays in a well-formed response, but nothing
  // upstream guarantees that at runtime.
  await showUsage(page, {
    enabled: true,
    provider: 'openai',
    total_analyses: 3,
    total_tokens_prompt: 0,
    total_tokens_completion: 0,
    total_tokens: 0,
    total_escalations: 0,
    total_escalation_tokens: 0,
    total_estimated_cost: null,
  })
  await expect(page.locator('#page-usage')).toBeVisible()
  await expect(page.locator('.app-nav-tab.active[data-tab="usage"]')).toBeVisible()
})

// ── Weekly / monthly spend ────────────────────────────────────────────

test('opens on the daily view without fetching the weekly/monthly rollups', async ({ page }) => {
  const hits = await showUsageWithPeriods(page)

  await expect(page.locator('#page-usage')).toContainText('Usage History')
  await expect(page.locator('#page-usage')).toContainText('Last 14 Days')
  await expect(historyRows(page).first()).toContainText('2026-01-04')
  // A tab nobody switches must not pay for a year-wide aggregate.
  expect(hits.periods).toBe(0)
})

test('switching to Week shows weekly spend, and to Month shows monthly, from one fetch', async ({ page }) => {
  const hits = await showUsageWithPeriods(page)

  await granularity(page).filter({ hasText: 'Week' }).click()
  await expect(page.locator('#page-usage')).toContainText('Last 12 Weeks')
  await expect(historyRows(page).first()).toContainText('Week 03, 2026')
  await expect(historyRows(page).first()).toContainText('$0.0143')
  expect(hits.periods).toBe(1)

  await granularity(page).filter({ hasText: 'Month' }).click()
  await expect(page.locator('#page-usage')).toContainText('Last 12 Months')
  await expect(historyRows(page).first()).toContainText('January 2026')
  await expect(historyRows(page).nth(1)).toContainText('December 2025')
  // Both granularities arrive together, so switching costs no extra request.
  expect(hits.periods).toBe(1)

  await granularity(page).filter({ hasText: 'Day' }).click()
  await expect(page.locator('#page-usage')).toContainText('Last 14 Days')
  await expect(historyRows(page).first()).toContainText('2026-01-04')
})

test('totals the selected period so the spend does not have to be added up by eye', async ({ page }) => {
  await showUsageWithPeriods(page)
  await granularity(page).filter({ hasText: 'Month' }).click()

  const total = page.locator('#page-usage .usage-total-row')
  await expect(total).toContainText('Total')
  await expect(total).toContainText('52') // 40 + 12 analyses
  await expect(total).toContainText('$0.0296') // 0.0228 + 0.0068
})

test('a failed rollup says so instead of showing an empty table', async ({ page }) => {
  await showUsageWithPeriods(page, usage(), null)

  await granularity(page).filter({ hasText: 'Week' }).click()
  await expect(page.locator('#page-usage')).toContainText('Could not load weekly usage.')
  await expect(historyRows(page)).toHaveCount(0)
})

test('the granularity control stays usable at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await showUsageWithPeriods(page)

  const week = granularity(page).filter({ hasText: 'Week' })
  await expect(week).toBeVisible()
  await week.click()
  await expect(historyRows(page).first()).toContainText('Week 03, 2026')

  // The page itself must not scroll sideways at phone width.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow).toBeLessThanOrEqual(0)
})

test('weekly spend renders in dark mode as well as light', async ({ page }) => {
  await showUsageWithPeriods(page)
  await granularity(page).filter({ hasText: 'Week' }).click()
  await expect(historyRows(page).first()).toContainText('Week 03, 2026')

  // Whichever theme the app starts in, the weekly table must read correctly
  // in the other one too — the rows and the total row are the parts that
  // carry custom colour (the total row's own border/weight).
  const total = page.locator('#page-usage .usage-total-row')
  await expect(total).toBeVisible()

  await page.getByRole('button', { name: /Switch to (light|dark) theme/ }).click()
  await expect(historyRows(page).first()).toContainText('Week 03, 2026')
  await expect(total).toBeVisible()

  await page.getByRole('button', { name: /Switch to (light|dark) theme/ }).click()
  await expect(historyRows(page).first()).toContainText('Week 03, 2026')
  await expect(total).toBeVisible()
})
