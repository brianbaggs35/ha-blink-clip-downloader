import { appApiUrl, test, expect } from './coverage-fixtures'

test.describe('@ha', () => {
  test('loads the app through Supervisor ingress', async ({ page }) => {
    const response = await page.goto('/')

    expect(response?.ok()).toBe(true)
    await page.locator('.app-nav-tab[data-tab="library"]').click()
    await expect(page.locator('.app-nav-tab.active[data-tab="library"]')).toBeVisible()
  })

  test('saves and reloads a camera setting through Supervisor ingress', async ({ page }) => {
    const configUrl = appApiUrl('/api/ai/camera-configs')
    const initialResponse = await page.request.get(configUrl)
    expect(initialResponse.ok()).toBe(true)
    const originalConfigs = (await initialResponse.json()) as {
      camera: string
      description?: string
    }[]

    try {
      await page.goto('/')
      await page.locator('.app-nav-tab[data-tab="ai"]').click()
      await expect(page.locator('.app-nav-tab.active[data-tab="ai"]')).toBeVisible()

      await page.locator('.p-accordionheader', { hasText: 'Front Door' }).click()
      const description = `HA ingress E2E ${Date.now()}`
      const descriptionInput = page.locator('[id="cam-desc-Front Door"]')
      await descriptionInput.fill(description)
      await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
      await expect(page.getByText('Camera configs saved')).toBeVisible()

      const savedResponse = await page.request.get(configUrl)
      expect(savedResponse.ok()).toBe(true)
      expect(await savedResponse.json()).toContainEqual(expect.objectContaining({ camera: 'Front Door', description }))

      await page.reload()
      await page.locator('.app-nav-tab[data-tab="ai"]').click()
      await page.locator('.p-accordionheader', { hasText: 'Front Door' }).click()
      await expect(descriptionInput).toHaveValue(description)
    } finally {
      const restoreResponse = await page.request.put(configUrl, {
        data: originalConfigs,
      })
      expect(restoreResponse.ok()).toBe(true)
    }
  })
})
