import { test, expect } from '@playwright/test'

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
