import { appApiUrl, test, expect } from './coverage-fixtures'
import type { Page } from '@playwright/test'

// The Security Assistant against the real, seeded add-on. Everything up to the
// provider is real: the report is built from the seeded database, and the
// question and history are redacted by the server's own code. Only the paid call
// is replaced (e2e_fixtures.py's e2e_chat), and it reports back what it was
// sent, so these tests can see that no enrolled name got through.
//
// A question writes a chat and a row of spend, so each test removes the chats it
// made and puts the settings back over HTTP -- never by navigating, which would
// throw away the coverage the test collected.

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="assistant"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="assistant"]')
  await expect(page.getByTestId('assistant-model-tag')).toBeVisible()
})

test.afterEach(async ({ page }) => {
  const chats = (await (await page.request.get(appApiUrl('/api/assistant/chats'))).json()) as Array<{ id: string }>
  for (const chat of chats) await page.request.delete(appApiUrl(`/api/assistant/chats/${chat.id}`))
  await page.request.put(appApiUrl('/api/assistant/settings'), { data: { model: '', days: 7 } })
})

async function ask(page: Page, question: string) {
  const input = page.getByTestId('assistant-input')
  await input.fill(question)
  await page.getByTestId('assistant-send').click()
}

const bubbles = (page: Page) => page.getByTestId('assistant-message')

test('opens on a welcome, names the provider and says what is never sent', async ({ page }) => {
  await expect(page.getByRole('heading', { name: 'Security Assistant' })).toBeVisible()
  await expect(page.getByTestId('assistant-model-tag')).toContainText('Ollama')
  await expect(page.getByTestId('assistant-model-tag')).toContainText('llava')
  await expect(page.locator('.assistant-privacy')).toContainText('on your own network')
  await expect(page.locator('.assistant-privacy')).toContainText(
    'Camera images, face photos and the names of people you have enrolled are never included.',
  )
  await expect(page.getByTestId('assistant-suggestion').first()).toBeVisible()
  await expect(page.getByText('No chats yet')).toBeVisible()
})

test('a question is answered, titled and kept, and no enrolled name reaches the provider', async ({ page }) => {
  await ask(page, 'Was Riley E2E at the door?')

  await expect(bubbles(page)).toHaveCount(2)
  const answer = bubbles(page).nth(1)
  // The server redacted the name before the provider saw the question.
  await expect(answer).toContainText('E2E answer to: Was a household member at the door?')
  await expect(answer).toContainText('[enrolled names sent: no]')
  await expect(answer).toContainText('[model: default]')
  await expect(page.getByTestId('assistant-chat-item').first()).toContainText('Was Riley E2E at the door?')
})

test('a clip the answer cites opens in the Library player without leaving the tab', async ({ page }) => {
  // Nothing seeded is analyzed (any analysis row would move the AI tabs' counts),
  // so the server has no clip to cite itself -- pytest covers that half. This adds
  // a citation of a real seeded clip to the real answer.
  const clips = (await (await page.request.get(appApiUrl('/api/clips'))).json()) as
    { clips?: Array<Record<string, string>> } | Array<Record<string, string>>
  const clip = (Array.isArray(clips) ? clips : (clips.clips ?? []))[0]!
  await page.route('**/api/assistant/chats/*/ask', async (route) => {
    const response = await route.fetch()
    const body = (await response.json()) as { messages: Array<Record<string, unknown>> }
    body.messages[1]!.clips = [
      { ref: 'C1', id: clip.id, camera: clip.camera, timestamp: clip.timestamp, is_suspicious: false },
    ]
    await route.fulfill({ response, json: body })
  })

  await ask(page, 'Show me the latest clip')
  const chip = page.getByTestId('assistant-clip-chip').first()
  await expect(chip).toBeVisible()
  await expect(chip).toContainText(clip.camera!)
  await chip.click()
  await expect(page.locator('.modal-bg.open')).toBeVisible()
  await expect(page.locator('.app-nav-tab.active[data-tab="assistant"]')).toBeVisible()
})

test('a chat survives a reload and keeps its messages', async ({ page }) => {
  await ask(page, 'How are the batteries?')
  await expect(bubbles(page)).toHaveCount(2)

  await page.reload()
  await page.locator('.app-nav-tab[data-tab="assistant"]').click()
  await page.getByTestId('assistant-chat-item').first().click()

  await expect(bubbles(page)).toHaveCount(2)
  await expect(bubbles(page).first()).toContainText('How are the batteries?')
})

test('a suggestion asks its question with one click', async ({ page }) => {
  const suggestion = page.getByTestId('assistant-suggestion').first()
  const text = (await suggestion.innerText()).trim()
  await suggestion.click()
  await expect(bubbles(page).first()).toContainText(text)
  await expect(bubbles(page)).toHaveCount(2)
})

test('chats can be started, renamed, switched between and deleted', async ({ page }) => {
  await ask(page, 'First question')
  await expect(bubbles(page)).toHaveCount(2)

  await page.getByTestId('assistant-new-chat').click()
  await expect(page.getByTestId('assistant-conversation')).toContainText('Ask about your security system')
  await ask(page, 'Second question')
  await expect(bubbles(page)).toHaveCount(2)
  await expect(page.getByTestId('assistant-chat-item')).toHaveCount(2)

  // Rename the first one from its menu.
  await page.getByRole('button', { name: 'Options for First question' }).click()
  await page.getByRole('menuitem', { name: 'Rename' }).click()
  const rename = page.getByTestId('assistant-rename-input')
  await rename.fill('Driveway watch')
  await rename.press('Enter')
  await expect(page.getByTestId('assistant-chat-item').filter({ hasText: 'Driveway watch' })).toBeVisible()

  // Switching shows that chat's own messages.
  await page.getByTestId('assistant-chat-item').filter({ hasText: 'Driveway watch' }).click()
  await expect(bubbles(page).first()).toContainText('First question')
  await page.getByTestId('assistant-chat-item').filter({ hasText: 'Second question' }).click()
  await expect(bubbles(page).first()).toContainText('Second question')

  // Delete the second, after confirming.
  await page.getByRole('button', { name: 'Options for Second question' }).click()
  await page.getByRole('menuitem', { name: 'Delete' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByTestId('assistant-chat-item')).toHaveCount(1)
  await expect(page.getByTestId('assistant-chat-item')).toContainText('Driveway watch')
})

test('Escape cancels a rename and leaves the name alone', async ({ page }) => {
  await ask(page, 'Keep this name')
  await expect(bubbles(page)).toHaveCount(2)
  await page.getByRole('button', { name: 'Options for Keep this name' }).click()
  await page.getByRole('menuitem', { name: 'Rename' }).click()
  const rename = page.getByTestId('assistant-rename-input')
  await rename.fill('Something else')
  await rename.press('Escape')
  await expect(page.getByTestId('assistant-chat-item')).toContainText('Keep this name')
})

test("the gear opens settings, the model comes from the provider's list, and the choice is used and kept", async ({
  page,
}) => {
  await page.getByTestId('assistant-settings-button').click()
  const dialog = page.getByRole('dialog', { name: 'Assistant settings' })
  await expect(dialog).toContainText('not the escalation provider')

  // Picked from the list, never typed.
  await dialog.getByTestId('assistant-model-select').click()
  const options = page.getByRole('option')
  await expect(options.filter({ hasText: 'e2e-small' })).toBeVisible()
  await options.filter({ hasText: 'e2e-large' }).click()
  await dialog.getByTestId('assistant-days').getByText('14 days').click()
  await dialog.getByTestId('assistant-settings-save').click()
  await expect(dialog).toBeHidden()

  await expect(page.getByTestId('assistant-model-tag')).toContainText('e2e-large')
  await ask(page, 'Which model answers?')
  await expect(bubbles(page).nth(1)).toContainText('[model: e2e-large]')
  await expect(bubbles(page).nth(1)).toContainText('14 days')

  const saved = await (await page.request.get(appApiUrl('/api/assistant/status'))).json()
  expect(saved.settings).toEqual({ model: 'e2e-large', days: 14 })
})

test("a model that is not in the provider's list cannot be saved", async ({ page }) => {
  const response = await page.request.put(appApiUrl('/api/assistant/settings'), {
    data: { model: 'typo-model', days: 7 },
  })
  expect(response.status()).toBe(400)
  const status = await (await page.request.get(appApiUrl('/api/assistant/status'))).json()
  expect(status.settings.model).toBe('')
})

test("the assistant's spend shows on the AI Usage tab", async ({ page }) => {
  await ask(page, 'Anything unusual?')
  await expect(bubbles(page)).toHaveCount(2)

  const usage = await (await page.request.get(appApiUrl('/api/ai/usage'))).json()
  expect(usage.assistant.questions).toBeGreaterThan(0)
  expect(usage.assistant.tokens_total).toBeGreaterThanOrEqual(1280)

  await page.locator('.app-nav-tab[data-tab="usage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="usage"]')
  await expect(page.getByTestId('usage-assistant-questions')).toBeVisible()
  await expect(page.getByTestId('usage-assistant-tokens')).toBeVisible()
  await expect(page.getByTestId('usage-assistant-questions')).toContainText('Assistant Questions')
})

test('chats and their spend are part of a backup', async ({ page }) => {
  await ask(page, 'Is this in the backup?')
  await expect(bubbles(page)).toHaveCount(2)

  const preview = (await (await page.request.get(appApiUrl('/api/backup/preview'))).json()) as {
    tables: Array<{ name: string; rows: number }>
  }
  for (const name of ['assistant_chats', 'assistant_usage']) {
    expect(preview.tables.find((t) => t.name === name)?.rows ?? 0, name).toBeGreaterThan(0)
  }

  await page.locator('.app-nav-tab[data-tab="backup"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="backup"]')
  await expect(page.locator('.include-list [data-table="assistant_chats"]')).toBeVisible()
})
