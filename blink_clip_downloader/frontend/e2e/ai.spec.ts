import { test, expect } from './coverage-fixtures'

// AI and AI Usage are both gated server-side on `analyzer is not None`
// (media_server.py's _handle_ai_status/_handle_ai_usage) -- standalone_server.py
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
  await page.locator('#cam-desc-Garage').fill(description)
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await expect(page.locator('#cam-desc-Garage')).toHaveValue(description)
})

test('AI Usage tab shows the configured provider with zero usage recorded', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')

  await expect(page.getByText('AI Token Usage')).toBeVisible()
  const providerCard = page.locator('.status-card', { hasText: 'Current Provider' })
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

test('Fetch Models finds none on the unreachable Ollama server, and Copy requires a selection first', async ({
  page,
}) => {
  // AiConnectionCard's model picker is shown for the ollama provider
  // (showModelPicker()) regardless of connectivity — fetchAiModels() below
  // makes a real request to the same unreachable port ai_online:false
  // already comes from. ClipAnalyzer.fetch_models() (analyzer.py) catches
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
