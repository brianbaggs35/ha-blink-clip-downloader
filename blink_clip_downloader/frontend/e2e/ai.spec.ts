import { test, expect } from './coverage-fixtures'

// AI and AI Usage are both gated server-side on `analyzer is not None`
// (media_server/ai.py's _handle_ai_status/_handle_ai_usage) -- standalone_server.py
// wires in a real ClipAnalyzer pointed at an unreachable port specifically
// so these two tabs have something real to render (Offline, not "not
// configured"), without needing an actual Ollama/cloud provider.

test('AI tab shows the configured (offline) provider and lets you edit per-camera configs', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await expect(page.getByText('AI Connection')).toBeVisible()
  await expect(page.getByText('Offline')).toBeVisible()
  await expect(page.getByText('Provider:')).toContainText('Ollama (Local/LAN)')

  await expect(page.getByText('Camera Configurations')).toBeVisible()
  const description = 'e2e test: points at the driveway'
  // Each camera's fields live behind a collapsed accordion panel now —
  // expand Garage's before it's fillable.
  await page.locator('.p-accordionheader', { hasText: 'Garage' }).click()
  await page.locator('#cam-desc-Garage').fill(description)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.locator('.p-accordionheader', { hasText: 'Garage' }).click()
  await expect(page.locator('#cam-desc-Garage')).toHaveValue(description)
})

test('a half-typed camera description survives a background refresh from another tab', async ({ page }) => {
  // The cross-tab refresh signal reloads this section from the server. It
  // has to skip that while the form is dirty, or an edit someone is still
  // typing is silently replaced by what is on disk. Deliberately leaves
  // nothing saved.
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await page.locator('.p-accordionheader', { hasText: 'Backyard' }).click()

  const typed = 'e2e unsaved draft, never saved'
  await page.locator('#cam-desc-Backyard').fill(typed)
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()

  await expect(page.locator('#cam-desc-Backyard')).toHaveValue(typed)
})

test('the Failed queue count opens a modal listing why each clip failed to analyze', async ({ page }) => {
  // standalone_server.py seeds one real failed analysis_queue row (on
  // e2e-failed-upload/Test Scratch), so /api/ai/queue/failed -- the
  // endpoint the modal itself calls -- returns real data from a real
  // query, no mocking needed. But the queue *count* AiStatusCards reads to
  // decide whether the button is even clickable only ever comes from a
  // real AnalysisQueue object (media_server/ai.py's _handle_ai_status), which
  // standalone_server.py deliberately never constructs (no queue-
  // processing loop runs against the unreachable analyzer -- see its own
  // module docstring) -- so status.queue.failed is always 0 from the real
  // endpoint. Mocking just that one field on top of the real response,
  // same approach as "the escalation tier shows..." below, makes the
  // button clickable without faking the part this test actually cares
  // about proving (the modal's real content).
  await page.route('**/api/ai/status', async (route) => {
    const response = await route.fetch()
    const status = (await response.json()) as Record<string, unknown>
    const queue = (status.queue as Record<string, unknown>) ?? {}
    await route.fulfill({ response, json: { ...status, queue: { ...queue, failed: 1 } } })
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  const failedButton = page.locator('.failed-stat-btn')
  await expect(failedButton).toBeEnabled()
  await failedButton.click()

  const dialog = page.getByRole('dialog', { name: 'Failed Analyses' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('Test Scratch')
  await expect(dialog).toContainText('Simulated failure for e2e testing')

  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).not.toBeVisible()
})

test('AI Analysis Configuration dialog toggles automatic analysis per camera and persists it', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await expect(page.getByText('cameras enabled for automatic analysis')).toBeVisible()
  await page.getByRole('button', { name: 'Configure Cameras' }).click()
  const dialog = page.getByRole('dialog', { name: 'AI Analysis Configuration' })
  await expect(dialog.getByText('Automatic analysis applies only to newly downloaded clips')).toBeVisible()

  const garageRow = dialog.locator('.camera-row', { has: page.locator('label[for="ai-analysis-garage"]') })
  await expect(garageRow).toContainText('Automatic analysis enabled')
  await page.locator('#ai-analysis-garage').click()
  await expect(garageRow).toContainText('Automatic analysis disabled')

  await dialog.getByRole('button', { name: 'Save Settings' }).click()
  await expect(page.getByText('AI analysis settings saved')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.getByRole('button', { name: 'Configure Cameras' }).click()
  await expect(page.locator('.camera-row', { has: page.locator('label[for="ai-analysis-garage"]') })).toContainText(
    'Automatic analysis disabled',
  )
})

test('AI Usage tab shows the configured provider with zero usage recorded', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')

  await expect(page.getByText('AI Token Usage')).toBeVisible()
  const providerCard = page.locator('.p-card', { hasText: 'Current Provider' })
  await expect(providerCard.locator('.status-row', { hasText: 'Provider' })).toContainText('Ollama (Local/LAN)')
  await expect(providerCard.locator('.status-row', { hasText: 'Model' })).toContainText('llava')
  await expect(page.getByText('No analysis data yet')).toBeVisible()
})

// Must run after the AI Usage test above: it creates a real analysis_results
// row, which would make "No analysis data yet" false if it ran first.
// Declaration order is execution order here (workers: 1, no intra-file
// parallelism), same convention storage.spec.ts's own mutating test uses.
test('Test Analysis runs a real analysis against the most recent clip', async ({ page }) => {
  // Exercises AiConnectionCard's own runTest()/testResult rendering, a
  // different frontend entry point into the same real analyze_clip code
  // path ClipAiPanel's "Analyze Now" already covers from the clip modal —
  // the most recent seeded clip has no real file on disk, so frame
  // extraction genuinely (not mocked) comes back empty, landing on the
  // "AI is working!" success branch with a real, deterministic result.
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await page.getByRole('button', { name: 'Test Analysis' }).click()
  await expect(page.getByText('✓ AI is working!')).toBeVisible()
  await expect(page.getByText('No frames could be extracted')).toBeVisible()
})

// Continues from the Test Analysis test above (declaration order is
// execution order, workers: 1): that real analysis already recorded real
// usage stats for e2e-clip-000, so this reaches the populated stats grid /
// per-model / daily tables the "zero usage" test above (which runs before
// Test Analysis) can't.
test('AI Usage tab reflects the completed analysis, and Clear Stats resets it', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')

  const statsGrid = page.locator('.usage-grid')
  await expect(statsGrid.locator('.usage-stat', { hasText: 'Clips Analyzed' }).locator('.num')).toHaveText('1')
  await expect(page.getByText('No analysis data yet')).toHaveCount(0)
  await expect(page.getByText('No analysis activity in the last 14 days')).toHaveCount(0)

  await page.getByRole('button', { name: '🗑 Clear Stats' }).click()
  await expect(page.getByText('Clear all AI usage stats')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()

  await expect(page.getByText('AI usage stats cleared')).toBeVisible()
  await expect(statsGrid.locator('.usage-stat', { hasText: 'Clips Analyzed' }).locator('.num')).toHaveText('0')
  await expect(page.getByText('No analysis data yet')).toBeVisible()
})

test('a failed Clear Stats says so instead of pretending the counters were reset', async ({ page }) => {
  // Deliberately runs after the test above, which has already cleared the
  // real counters — routing the DELETE to a 500 means this one changes
  // nothing server-side, so it cannot perturb anything that follows.
  await page.route('**/api/ai/usage', (route) =>
    route.request().method() === 'DELETE' ? route.fulfill({ status: 500, body: 'boom' }) : route.continue(),
  )
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')

  await page.getByRole('button', { name: '🗑 Clear Stats' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText('Failed to clear usage stats')).toBeVisible()
})

test('Fetch Models finds none on the unreachable Ollama server, and Copy requires a selection first', async ({
  page,
}) => {
  // AiConnectionCard's model picker is shown for the ollama provider
  // (showModelPicker()) regardless of connectivity — fetchAiModels() below
  // makes a real request to the same unreachable port ai_online:false
  // already comes from. ClipAnalyzer.fetch_models()
  // (analyzer/ollama_provider.py) catches
  // the connection error itself and returns an empty list rather than
  // raising, so this genuinely lands on the "found none" branch, not a
  // request failure — same graceful-empty-result shape Test Analysis above
  // already exercises for analyze_clip.
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await page.getByRole('button', { name: '📋 Copy' }).click()
  await expect(page.getByText('Fetch models and pick one first')).toBeVisible()

  await page.getByRole('button', { name: '⟳ Fetch Models' }).click()
  await expect(page.getByText('No vision models found on this Ollama server')).toBeVisible()
})

test('AI Usage tab shows the right explanatory note for each remaining provider', async ({ page }) => {
  // Every other provider's own AI Usage test (above, and in
  // mocked-integrations.spec.ts) mounts with provider 'ollama' or
  // 'moondream_cloud' — this is the only place the other four providers'
  // ProviderNote.vue text blocks get exercised at all.
  const providers = ['ollama_cloud', 'anthropic', 'openai', 'moondream_local']
  const expectedText: Record<string, string> = {
    ollama_cloud: 'hosted Ollama service',
    anthropic: 'Anthropic (Claude) charges per token',
    openai: 'OpenAI charges per token',
    moondream_local: 'Moondream Local runs entirely on-device',
  }

  for (const provider of providers) {
    await page.route('**/api/ai/usage', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: true,
          provider,
          model: 'test-model',
          total_analyses: 1,
          total_tokens: 0,
          total_escalations: 0,
          by_model: [],
          daily: [],
        }),
      }),
    )
    await page.goto('/')
    await page.locator('.app-nav-tab[data-tab="usage"]').click()
    await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')
    await expect(page.getByText(expectedText[provider])).toBeVisible()
  }
})

// AiConnectionCard's escalation (tier 2) block only renders when the
// mounted status has an escalation_provider — the real standalone-server
// analyzer has none configured, so every test above (and the AI tab's
// default state) never touches it. Mocking /api/ai/status is the only way
// to reach it without a second real analyzer. (The online case, plus
// escalation fetch/copy, are already covered by mocked-integrations.spec.ts
// — "shows the escalation model as online..." and the fetch/copy steps
// inside "covers AI configuration, feedback, email alerts..." — so only the
// offline/fallback state is added here.)
test('the escalation tier shows the unreachable fallback note when offline', async ({ page }) => {
  await page.route('**/api/ai/status', async (route) => {
    const response = await route.fetch()
    const status = (await response.json()) as Record<string, unknown>
    await route.fulfill({
      response,
      json: { ...status, escalation_provider: 'anthropic', escalation_model: 'claude-test', escalation_online: false },
    })
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await expect(page.getByText('🔴 unreachable — falling back to tier 1')).toBeVisible()
})

// The unsupported-architecture state and the full idle->installing->failed/
// installed install flow are already covered by mocked-integrations.spec.ts
// ("shows the unsupported-architecture state...", "completes a mocked
// Moondream local install...", "shows the failed state..."). This is the one
// moondream scenario neither of those hits: arriving with the package
// already installed, which takes a genuinely different code path (the
// immediate provider watcher's `props.status.moondream_installed ? ... `
// branch) rather than pollMoondreamStatus()'s `if (s.installed)` branch the
// install-flow tests exercise instead.
test('moondream_local shows installed when already installed', async ({ page }) => {
  await page.route('**/api/ai/status', async (route) => {
    const response = await route.fetch()
    const status = (await response.json()) as Record<string, unknown>
    await route.fulfill({
      response,
      json: { ...status, provider: 'moondream_local', moondream_installed: true },
    })
  })
  await page.route('**/api/ai/moondream/install-status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ arch_supported: true, installed: true }),
    }),
  )

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await expect(page.getByText('✓ moondream installed')).toBeVisible()
})

test('the AI tab survives a backend that returns nothing for its queue', async ({ page }) => {
  // A 6.0.0 upgrade adds columns to analysis_queue; an install whose
  // database somehow answers with nothing at all (a proxy, a half-applied
  // migration) must still render the tab rather than a blank page, since
  // this is the tab a user would go to in order to notice the problem.
  await page.route('**/api/ai/queue', (route) => route.fulfill({ json: {} }))
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await expect(page.getByText('AI Connection')).toBeVisible()
  await expect(page.getByText('Queue Status')).toBeVisible()
})
