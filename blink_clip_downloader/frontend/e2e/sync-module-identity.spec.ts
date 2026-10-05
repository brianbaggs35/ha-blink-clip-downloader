import { appApiUrl, test, expect } from './coverage-fixtures'

// What the Sync Module tab and the arm endpoints promise about *which*
// camera and *what* state, against the real server routes. The fake sync
// module behind them (scripts/standalone_server.py) resolves a camera the
// same way the real downloader does: by the ids the tab sends, before the
// name in the URL.

interface Module {
  armed: boolean
  cameras: { name: string; armed: boolean }[]
}

async function modules(page: import('@playwright/test').Page): Promise<Module[]> {
  const response = await page.request.get(appApiUrl('/api/sync-modules'))
  return response.json()
}

test('a camera renamed since the page loaded is still the one that gets armed @standalone', async ({ page }) => {
  // The tab is showing "Porch" for a camera Blink called "Front Door" a
  // moment ago. Its id has not changed, and that is what the tab sends.
  await page.route('**/api/sync-modules', async (route) => {
    const response = await route.fetch()
    const body: Module[] = await response.json()
    for (const camera of body[0]!.cameras) if (camera.name === 'Front Door') camera.name = 'Porch'
    await route.fulfill({ response, json: body })
  })
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="syncmodule"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="syncmodule"]')

  const porchCard = page.locator('.sm-cam-card', { hasText: 'Porch' })
  await porchCard.locator('input[role="switch"]').click()
  await expect(page.getByText('Porch disarmed')).toBeVisible()
  await expect(porchCard.getByText('Disarmed', { exact: true })).toBeVisible()

  // The real camera was found by its id, not by a name that matches nothing.
  await page.unroute('**/api/sync-modules')
  const frontDoor = (await modules(page))[0]!.cameras.find((c) => c.name === 'Front Door')
  expect(frontDoor?.armed).toBe(false)

  // Leave the shared backend as every other spec expects it, over HTTP
  // rather than the UI: a navigation here would throw this test's coverage away.
  await page.request.post(appApiUrl('/api/sync-modules/cameras/Front%20Door/arm'), { data: { armed: true } })
  expect((await modules(page))[0]!.cameras.every((c) => c.armed)).toBe(true)
})

test('an arm request that does not say true or false is refused, not read as disarm @standalone', async ({ page }) => {
  for (const data of [{}, { arm: true }, { armed: 'false' }, { armed: 0 }, { armed: null }]) {
    const module = await page.request.post(appApiUrl('/api/sync-modules/Home/arm'), { data })
    const camera = await page.request.post(appApiUrl('/api/sync-modules/cameras/Front%20Door/arm'), { data })
    expect([module.status(), camera.status()]).toEqual([400, 400])
  }

  const [home] = await modules(page)
  expect(home!.armed).toBe(true)
  expect(home!.cameras.every((c) => c.armed)).toBe(true)
})

test('an id that matches nothing is a 404, not an arm of whatever shares the name @standalone', async ({ page }) => {
  const camera = await page.request.post(appApiUrl('/api/sync-modules/cameras/Front%20Door/arm'), {
    data: { armed: false, camera_id: 'no-such-camera' },
  })
  const module = await page.request.post(appApiUrl('/api/sync-modules/Home/arm'), {
    data: { armed: false, network_id: 99999 },
  })
  expect([camera.status(), module.status()]).toEqual([404, 404])

  const [home] = await modules(page)
  expect(home!.armed).toBe(true)
  expect(home!.cameras.every((c) => c.armed)).toBe(true)
})
