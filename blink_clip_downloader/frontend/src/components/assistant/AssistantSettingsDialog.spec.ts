import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AssistantSettingsDialog from './AssistantSettingsDialog.vue'
import { ApiError } from '../../api/client'
import type { AssistantStatus } from '../../api/types'
import { useToastStore } from '../../stores/toast'

vi.mock('../../api/assistant', () => ({
  getAssistantModels: vi.fn(),
  saveAssistantSettings: vi.fn(),
}))
import { getAssistantModels, saveAssistantSettings } from '../../api/assistant'

const STATUS: AssistantStatus = {
  enabled: true,
  provider: 'anthropic',
  default_model: 'claude-default',
  supported: true,
  sends_off_device: true,
  settings: { model: '', days: 7 },
  days_choices: [1, 3, 7],
  chat_limit: 50,
  message_limit: 200,
}

function mountDialog(status: AssistantStatus | null = STATUS) {
  return mount(AssistantSettingsDialog, {
    props: { visible: false, status, 'onUpdate:visible': () => undefined },
    attachTo: document.body,
  })
}

async function open(w: ReturnType<typeof mountDialog>) {
  await w.setProps({ visible: true })
  await flushPromises()
}

const body = () => document.body

describe('AssistantSettingsDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    document.body.innerHTML = ''
    vi.mocked(getAssistantModels)
      .mockReset()
      .mockResolvedValue({
        enabled: true,
        models: ['claude-a', 'claude-b'],
        default: 'claude-default',
        provider: 'anthropic',
      })
    vi.mocked(saveAssistantSettings)
      .mockReset()
      .mockResolvedValue({
        saved: true,
        settings: { model: 'claude-b', days: 3 },
      })
  })

  it('names the model picker by its label, for screen readers and for the label click', async () => {
    const w = mountDialog()
    await open(w)
    const label = body().querySelector('label[for="assistant-model"]')
    expect(label?.id).toBe('assistant-model-label')
    const input = body().querySelector('#assistant-model')
    expect(input?.getAttribute('aria-labelledby')).toBe('assistant-model-label')
    w.unmount()
  })

  it('stays closed and loads nothing until opened', () => {
    mountDialog()
    expect(getAssistantModels).not.toHaveBeenCalled()
    expect(body().querySelector('[data-testid="assistant-settings"]')).toBeNull()
  })

  it('explains that the base provider is used, and loads its models when opened', async () => {
    const w = mountDialog()
    await open(w)
    expect(getAssistantModels).toHaveBeenCalledTimes(1)
    const text = body().textContent ?? ''
    expect(text).toContain('Anthropic')
    expect(text).toContain('not the escalation provider')
    expect(text).toContain('Provider default (claude-default)')
  })

  it('saves the chosen model and window, then closes and reports them', async () => {
    const w = mountDialog({ ...STATUS, settings: { model: 'claude-b', days: 3 } })
    await open(w)
    ;(body().querySelector('[data-testid="assistant-settings-save"]') as HTMLElement).click()
    await flushPromises()
    expect(saveAssistantSettings).toHaveBeenCalledWith({ model: 'claude-b', days: 3 })
    expect(w.emitted('saved')).toEqual([[{ model: 'claude-b', days: 3 }]])
    expect(w.emitted('update:visible')?.at(-1)).toEqual([false])
    expect(useToastStore().message).toBe('Assistant settings saved')
  })

  it('shows the server’s reason when saving fails and stays open', async () => {
    vi.mocked(saveAssistantSettings).mockRejectedValue(
      new ApiError(400, '400: x', 'That model is not in the provider’s list.'),
    )
    const w = mountDialog()
    await open(w)
    ;(body().querySelector('[data-testid="assistant-settings-save"]') as HTMLElement).click()
    await flushPromises()
    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toBe('That model is not in the provider’s list.')
    expect(w.emitted('saved')).toBeUndefined()
  })

  it('warns when the provider cannot list its models', async () => {
    vi.mocked(getAssistantModels).mockResolvedValue({ enabled: true, models: [], default: 'x' })
    const w = mountDialog()
    await open(w)
    expect(body().textContent).toContain('model list could not be loaded')
  })

  it('warns when the model request itself fails', async () => {
    vi.mocked(getAssistantModels).mockRejectedValue(new Error('offline'))
    const w = mountDialog()
    await open(w)
    expect(body().textContent).toContain('model list could not be loaded')
  })

  it('starts from the saved values each time it opens, and copes with no status', async () => {
    const w = mountDialog(null)
    await open(w)
    expect(body().textContent).toContain('Provider default (not set)')
    expect(body().textContent).toContain('7 days')
  })

  it('Cancel closes without saving', async () => {
    const w = mountDialog()
    await open(w)
    const cancel = [...body().querySelectorAll('button')].find((b) => b.textContent?.includes('Cancel'))
    cancel?.click()
    await flushPromises()
    expect(saveAssistantSettings).not.toHaveBeenCalled()
    expect(w.emitted('update:visible')?.at(-1)).toEqual([false])
  })

  it('labels a one-day window in the singular', async () => {
    const w = mountDialog()
    await open(w)
    expect(body().textContent).toContain('1 day')
  })

  it('saves a model picked from the list and a window picked from the buttons', async () => {
    const w = mountDialog()
    await open(w)
    ;(body().querySelector('.p-select') as HTMLElement).click()
    await flushPromises()
    const option = [...body().querySelectorAll('.p-select-option')].find((o) => o.textContent?.includes('claude-b'))
    ;(option as HTMLElement).click()
    await flushPromises()
    const oneDay = [...body().querySelectorAll('.p-togglebutton')].find((b) => b.textContent?.includes('1 day'))
    ;(oneDay as HTMLElement).click()
    await flushPromises()
    ;(body().querySelector('[data-testid="assistant-settings-save"]') as HTMLElement).click()
    await flushPromises()
    expect(saveAssistantSettings).toHaveBeenCalledWith({ model: 'claude-b', days: 1 })
  })

  it('closes from the dialog’s own close button', async () => {
    const w = mountDialog()
    await open(w)
    ;(body().querySelector('.p-dialog-close-button') as HTMLElement).click()
    await flushPromises()
    expect(w.emitted('update:visible')?.at(-1)).toEqual([false])
  })
})
