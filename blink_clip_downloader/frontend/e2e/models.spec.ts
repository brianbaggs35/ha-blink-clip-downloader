import { test, expect } from '@playwright/test'

// Purely static reference content -- no fetch on mount (see ModelsPage.vue)
// -- so this just confirms the real component actually mounted and
// rendered its real template, not a broken/blank page.
test('renders the AI providers reference content', async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="models"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="models"]')

  await expect(page.getByText('AI Providers & Models')).toBeVisible()
  // Not { exact: true }: each provider name renders inside an <h3> next to
  // a sibling <code> tag (e.g. "Ollama (Local/LAN) ollama"), so the
  // element's full text content is never *exactly* just the provider name.
  for (const provider of ['Ollama (Local/LAN)', 'Ollama Cloud', 'Anthropic (Claude)', 'OpenAI (GPT)']) {
    await expect(page.getByText(provider).first()).toBeVisible()
  }
})
