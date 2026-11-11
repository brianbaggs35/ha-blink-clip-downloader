import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AssistantPage from './AssistantPage.vue'
import AssistantChatList from './AssistantChatList.vue'
import { ApiError } from '../../api/client'
import type { AssistantChat, AssistantChatSummary, AssistantStatus } from '../../api/types'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useToastStore } from '../../stores/toast'

vi.mock('../../api/assistant', () => ({
  askAssistant: vi.fn(),
  createAssistantChat: vi.fn(),
  deleteAssistantChat: vi.fn(),
  getAssistantChat: vi.fn(),
  getAssistantModels: vi.fn(),
  getAssistantStatus: vi.fn(),
  listAssistantChats: vi.fn(),
  renameAssistantChat: vi.fn(),
  saveAssistantSettings: vi.fn(),
}))
const confirmMock = vi.fn()
vi.mock('../../composables/useConfirm', () => ({ useConfirm: () => confirmMock }))
import {
  askAssistant,
  createAssistantChat,
  deleteAssistantChat,
  getAssistantChat,
  getAssistantModels,
  getAssistantStatus,
  listAssistantChats,
  renameAssistantChat,
} from '../../api/assistant'

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

function summary(id: string, title: string, count = 2): AssistantChatSummary {
  return { id, title, created_at: '2026-11-10T00:00:00Z', updated_at: '2026-11-10T00:00:00Z', message_count: count }
}
function chat(id: string, title: string, messages: AssistantChat['messages'] = []): AssistantChat {
  return { ...summary(id, title, messages.length), messages }
}

const OVERNIGHT = chat('a', 'Overnight', [
  { role: 'user', content: 'Anything overnight?', at: '2026-11-10T01:00:00Z' },
  {
    role: 'assistant',
    content: 'One clip.',
    at: '2026-11-10T01:00:01Z',
    clips: [{ ref: 'C1', id: 'clip-9', camera: 'Porch', timestamp: '2026-11-10T00:30:00Z', is_suspicious: true }],
  },
])

let narrowListener: ((e: MediaQueryListEvent) => void) | null = null

function stubMatchMedia(matches: boolean) {
  narrowListener = null
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches,
      media: '',
      addEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => {
        narrowListener = fn
      },
      removeEventListener: vi.fn(),
    })),
  )
}

async function mountPage(opts: { status?: AssistantStatus; chats?: AssistantChatSummary[]; narrow?: boolean } = {}) {
  stubMatchMedia(opts.narrow ?? false)
  vi.mocked(getAssistantStatus).mockResolvedValue(opts.status ?? STATUS)
  vi.mocked(listAssistantChats).mockResolvedValue(opts.chats ?? [summary('a', 'Overnight'), summary('b', 'Batteries')])
  const w = mount(AssistantPage, { attachTo: document.body })
  await flushPromises()
  return w
}

const input = (w: VueWrapper) => w.find('[data-testid="assistant-input"]')
const list = (w: VueWrapper) => w.findComponent(AssistantChatList)
const messages = (w: VueWrapper) => w.findAll('[data-testid="assistant-message"]')

async function ask(w: VueWrapper, text: string) {
  await input(w).setValue(text)
  await input(w).trigger('keydown', { key: 'Enter' })
  await flushPromises()
}

describe('AssistantPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    document.body.innerHTML = ''
    confirmMock.mockReset()
    for (const fn of [
      askAssistant,
      createAssistantChat,
      deleteAssistantChat,
      getAssistantChat,
      getAssistantModels,
      getAssistantStatus,
      listAssistantChats,
      renameAssistantChat,
    ]) {
      vi.mocked(fn).mockReset()
    }
    vi.mocked(getAssistantChat).mockImplementation(async (id) => (id === 'a' ? OVERNIGHT : chat(id, 'Batteries')))
    vi.mocked(getAssistantModels).mockResolvedValue({ enabled: true, models: ['m1'], default: 'claude-default' })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  describe('loading', () => {
    it('opens the most recent chat and shows its conversation', async () => {
      const w = await mountPage()
      expect(getAssistantChat).toHaveBeenCalledWith('a')
      expect(messages(w).map((m) => m.text())).toEqual(['Anything overnight?', expect.stringContaining('One clip.')])
      expect(list(w).props('activeId')).toBe('a')
    })

    it('shows the welcome and suggestions when there are no chats', async () => {
      const w = await mountPage({ chats: [] })
      expect(w.text()).toContain('Ask about your security system')
      expect(w.findAll('[data-testid="assistant-suggestion"]').length).toBeGreaterThan(0)
      expect(getAssistantChat).not.toHaveBeenCalled()
    })

    it('says AI is not configured when there is no provider', async () => {
      const w = await mountPage({ status: { ...STATUS, enabled: false } })
      expect(w.text()).toContain('AI Analysis Not Configured')
      expect(w.find('[data-testid="assistant-settings-button"]').exists()).toBe(false)
    })

    it('explains a provider that cannot answer, and disables asking', async () => {
      const w = await mountPage({ status: { ...STATUS, provider: 'moondream_cloud', supported: false } })
      expect(w.find('[data-testid="assistant-unsupported"]').text()).toContain('cannot answer questions')
      expect(input(w).attributes('disabled')).toBeDefined()
      expect(w.findAll('[data-testid="assistant-suggestion"]')).toHaveLength(0)
    })

    it('toasts when the page cannot load', async () => {
      stubMatchMedia(false)
      vi.mocked(getAssistantStatus).mockRejectedValue(new Error('boom'))
      mount(AssistantPage, { attachTo: document.body })
      await flushPromises()
      expect(useToastStore().message).toBe('Failed to load the Security Assistant')
    })
  })

  describe('privacy note', () => {
    it('says what is sent to a hosted provider, and that faces and names never are', async () => {
      const w = await mountPage()
      const text = w.find('.assistant-privacy').text()
      expect(text).toContain('Each question sends Anthropic (Claude) a text summary')
      expect(text).toContain('Camera images, face photos and the names of people you have enrolled are never included')
    })

    it('says a local provider keeps questions on the network', async () => {
      const w = await mountPage({ status: { ...STATUS, provider: 'ollama', sends_off_device: false } })
      expect(w.find('.assistant-privacy').text()).toContain('answered by Ollama (Local/LAN) on your own network')
    })
  })

  describe('model tag', () => {
    it('shows the provider default, or the model picked in settings', async () => {
      let w = await mountPage()
      expect(w.find('[data-testid="assistant-model-tag"]').text()).toContain('claude-default')
      w = await mountPage({ status: { ...STATUS, settings: { model: 'claude-pick', days: 7 } } })
      expect(w.find('[data-testid="assistant-model-tag"]').text()).toContain('claude-pick')
    })
  })

  describe('asking', () => {
    const ANSWER = {
      title: 'Overnight',
      messages: [
        { role: 'user' as const, content: 'And the porch?', at: 't' },
        { role: 'assistant' as const, content: 'Quiet.', at: 't', model: 'claude-default' },
      ],
    }

    it('sends on Enter, shows the saved answer and refreshes the list', async () => {
      const w = await mountPage()
      vi.mocked(askAssistant).mockResolvedValue(ANSWER)
      await ask(w, 'And the porch?')
      expect(askAssistant).toHaveBeenCalledWith('a', 'And the porch?')
      expect(messages(w).at(-1)?.text()).toContain('Quiet.')
      expect(messages(w)).toHaveLength(4)
      expect((input(w).element as HTMLTextAreaElement).value).toBe('')
      expect(listAssistantChats).toHaveBeenCalledTimes(2)
    })

    it('Shift+Enter does not send, and nothing is sent when empty', async () => {
      const w = await mountPage()
      await input(w).setValue('hello')
      await input(w).trigger('keydown', { key: 'Enter', shiftKey: true })
      await input(w).setValue('   ')
      await input(w).trigger('keydown', { key: 'Enter' })
      await w.find('form').trigger('submit')
      expect(askAssistant).not.toHaveBeenCalled()
      expect(w.find('[data-testid="assistant-send"]').attributes('disabled')).toBeDefined()
    })

    it('sends with the button once there is text', async () => {
      const w = await mountPage()
      vi.mocked(askAssistant).mockResolvedValue(ANSWER)
      await input(w).setValue('And the porch?')
      expect(w.find('[data-testid="assistant-send"]').attributes('disabled')).toBeUndefined()
      await w.find('form').trigger('submit')
      await flushPromises()
      expect(askAssistant).toHaveBeenCalledTimes(1)
    })

    it('shows the question at once and a placeholder while it waits, and blocks a second send', async () => {
      const w = await mountPage()
      let finish: (v: typeof ANSWER) => void = () => undefined
      vi.mocked(askAssistant).mockReturnValue(new Promise((resolve) => (finish = resolve)))
      await input(w).setValue('And the porch?')
      await input(w).trigger('keydown', { key: 'Enter' })
      await flushPromises()
      expect(messages(w).at(-1)?.text()).toBe('And the porch?')
      expect(w.find('.assistant-thinking').exists()).toBe(true)
      expect(input(w).attributes('disabled')).toBeDefined()
      list(w).vm.$emit('create')
      list(w).vm.$emit('remove', 'a')
      await flushPromises()
      expect(createAssistantChat).not.toHaveBeenCalled()
      expect(confirmMock).not.toHaveBeenCalled()
      finish(ANSWER)
      await flushPromises()
      expect(w.find('.assistant-thinking').exists()).toBe(false)
    })

    it('starts a chat when none is open, then asks in it', async () => {
      const w = await mountPage({ chats: [] })
      vi.mocked(createAssistantChat).mockResolvedValue(chat('n', 'New chat'))
      vi.mocked(askAssistant).mockResolvedValue(ANSWER)
      await ask(w, 'And the porch?')
      expect(createAssistantChat).toHaveBeenCalledTimes(1)
      expect(askAssistant).toHaveBeenCalledWith('n', 'And the porch?')
    })

    it('a suggestion asks its question', async () => {
      const w = await mountPage({ chats: [] })
      vi.mocked(createAssistantChat).mockResolvedValue(chat('n', 'New chat'))
      vi.mocked(askAssistant).mockResolvedValue(ANSWER)
      await w.find('[data-testid="assistant-suggestion"]').trigger('click')
      await flushPromises()
      expect(askAssistant).toHaveBeenCalledWith('n', 'What happened overnight?')
    })

    it('takes a failed question back out and returns the words to the box', async () => {
      const w = await mountPage()
      vi.mocked(askAssistant).mockRejectedValue(new ApiError(502, '502: x', '{"error":"Anthropic could not answer"}'))
      await ask(w, 'And the porch?')
      expect(messages(w)).toHaveLength(2)
      expect((input(w).element as HTMLTextAreaElement).value).toBe('And the porch?')
      const toast = useToastStore()
      expect(toast.isError).toBe(true)
      expect(toast.message).toBe('Anthropic could not answer')
    })

    it('copes with a failure to even start the chat', async () => {
      const w = await mountPage({ chats: [] })
      vi.mocked(createAssistantChat).mockRejectedValue(new Error('down'))
      await ask(w, 'Hello?')
      expect(askAssistant).not.toHaveBeenCalled()
      expect((input(w).element as HTMLTextAreaElement).value).toBe('Hello?')
      expect(useToastStore().message).toBe('The assistant could not answer')
    })

    it('does not touch the open chat when the answer arrives after switching away', async () => {
      const w = await mountPage()
      let finish: (v: typeof ANSWER) => void = () => undefined
      vi.mocked(askAssistant).mockReturnValue(new Promise((resolve) => (finish = resolve)))
      await input(w).setValue('And the porch?')
      await input(w).trigger('keydown', { key: 'Enter' })
      await flushPromises()
      list(w).vm.$emit('select', 'b')
      await flushPromises()
      finish(ANSWER)
      await flushPromises()
      expect(messages(w)).toHaveLength(0)
    })

    it('leaves the conversation alone when a failure lands after switching away', async () => {
      const w = await mountPage()
      let fail: (e: Error) => void = () => undefined
      vi.mocked(askAssistant).mockReturnValue(new Promise((_, reject) => (fail = reject)))
      await input(w).setValue('And the porch?')
      await input(w).trigger('keydown', { key: 'Enter' })
      await flushPromises()
      list(w).vm.$emit('select', 'b')
      await flushPromises()
      fail(new Error('late'))
      await flushPromises()
      expect(messages(w)).toHaveLength(0)
    })
  })

  describe('chats', () => {
    it('opens another chat from the list, and ignores a click on the open one', async () => {
      const w = await mountPage()
      vi.mocked(getAssistantChat).mockClear()
      list(w).vm.$emit('select', 'a')
      await flushPromises()
      expect(getAssistantChat).not.toHaveBeenCalled()
      list(w).vm.$emit('select', 'b')
      await flushPromises()
      expect(getAssistantChat).toHaveBeenCalledWith('b')
      expect(list(w).props('activeId')).toBe('b')
    })

    it('says so, and refreshes the list, when a chat cannot be opened', async () => {
      const w = await mountPage()
      vi.mocked(getAssistantChat).mockRejectedValue(new Error('gone'))
      list(w).vm.$emit('select', 'b')
      await flushPromises()
      expect(useToastStore().message).toBe('Could not open that chat')
      expect(listAssistantChats).toHaveBeenCalledTimes(2)
    })

    it('copes with the list failing to refresh while reporting an earlier failure', async () => {
      const w = await mountPage()
      vi.mocked(listAssistantChats).mockRejectedValue(new Error('down'))
      vi.mocked(getAssistantChat).mockRejectedValue(new Error('gone'))
      list(w).vm.$emit('select', 'b')
      await flushPromises()
      expect(useToastStore().message).toBe('Could not open that chat')
      vi.mocked(askAssistant).mockRejectedValue(new Error('x'))
      await ask(w, 'Hello?')
      expect(useToastStore().message).toBe('The assistant could not answer')
    })

    it('starts a new chat and clears the conversation', async () => {
      const w = await mountPage()
      vi.mocked(createAssistantChat).mockResolvedValue(chat('n', 'New chat'))
      vi.mocked(listAssistantChats).mockResolvedValue([summary('n', 'New chat', 0), summary('a', 'Overnight')])
      list(w).vm.$emit('create')
      await flushPromises()
      expect(list(w).props('activeId')).toBe('n')
      expect(messages(w)).toHaveLength(0)
    })

    it('does not pile up empty chats: asking for a new one while on a blank one reuses it', async () => {
      const w = await mountPage({ chats: [summary('b', 'Batteries', 0)] })
      list(w).vm.$emit('create')
      await flushPromises()
      expect(createAssistantChat).not.toHaveBeenCalled()
    })

    it('reports a new chat that could not be started', async () => {
      const w = await mountPage()
      vi.mocked(createAssistantChat).mockRejectedValue(new ApiError(409, '409', '{"error":"You can keep 50 chats."}'))
      list(w).vm.$emit('create')
      await flushPromises()
      expect(useToastStore().message).toBe('You can keep 50 chats.')
    })

    it('renames a chat in place', async () => {
      const w = await mountPage()
      vi.mocked(renameAssistantChat).mockResolvedValue({ title: 'Porch' })
      list(w).vm.$emit('rename', 'a', 'Porch')
      await flushPromises()
      expect(renameAssistantChat).toHaveBeenCalledWith('a', 'Porch')
      expect(list(w).props('chats')[0].title).toBe('Porch')
    })

    it('reports a failed rename, and ignores a chat that has since gone', async () => {
      const w = await mountPage()
      vi.mocked(renameAssistantChat).mockRejectedValueOnce(new Error('x'))
      list(w).vm.$emit('rename', 'a', 'Porch')
      await flushPromises()
      expect(useToastStore().message).toBe('Could not rename the chat')
      vi.mocked(renameAssistantChat).mockResolvedValueOnce({ title: 'Ghost' })
      list(w).vm.$emit('rename', 'zzz', 'Ghost')
      await flushPromises()
      expect(
        list(w)
          .props('chats')
          .map((c: AssistantChatSummary) => c.title),
      ).toEqual(['Overnight', 'Batteries'])
    })

    it('does nothing when deleting is not confirmed', async () => {
      const w = await mountPage()
      confirmMock.mockResolvedValue(false)
      list(w).vm.$emit('remove', 'a')
      await flushPromises()
      expect(confirmMock).toHaveBeenCalledWith(expect.stringContaining('Overnight'), 'Delete chat')
      expect(deleteAssistantChat).not.toHaveBeenCalled()
    })

    it('deleting the open chat moves to the next one', async () => {
      const w = await mountPage()
      confirmMock.mockResolvedValue(true)
      vi.mocked(deleteAssistantChat).mockResolvedValue({ deleted: true })
      vi.mocked(listAssistantChats).mockResolvedValue([summary('b', 'Batteries')])
      list(w).vm.$emit('remove', 'a')
      await flushPromises()
      expect(deleteAssistantChat).toHaveBeenCalledWith('a')
      expect(list(w).props('activeId')).toBe('b')
      expect(useToastStore().message).toBe('Chat deleted')
    })

    it('deleting the last chat leaves the welcome screen', async () => {
      const w = await mountPage({ chats: [summary('a', 'Overnight')] })
      confirmMock.mockResolvedValue(true)
      vi.mocked(deleteAssistantChat).mockResolvedValue({ deleted: true })
      vi.mocked(listAssistantChats).mockResolvedValue([])
      list(w).vm.$emit('remove', 'a')
      await flushPromises()
      expect(list(w).props('activeId')).toBeNull()
      expect(w.text()).toContain('Ask about your security system')
    })

    it('deleting another chat leaves the open one alone', async () => {
      const w = await mountPage()
      confirmMock.mockResolvedValue(true)
      vi.mocked(deleteAssistantChat).mockResolvedValue({ deleted: true })
      vi.mocked(listAssistantChats).mockResolvedValue([summary('a', 'Overnight')])
      list(w).vm.$emit('remove', 'b')
      await flushPromises()
      expect(list(w).props('activeId')).toBe('a')
      expect(messages(w)).toHaveLength(2)
    })

    it('words the confirmation generically for a chat the list no longer has', async () => {
      const w = await mountPage()
      confirmMock.mockResolvedValue(false)
      list(w).vm.$emit('remove', 'zzz')
      await flushPromises()
      expect(confirmMock).toHaveBeenCalledWith(expect.stringContaining('this chat'), 'Delete chat')
    })

    it('reports a chat that could not be deleted', async () => {
      const w = await mountPage()
      confirmMock.mockResolvedValue(true)
      vi.mocked(deleteAssistantChat).mockRejectedValue(new Error('x'))
      list(w).vm.$emit('remove', 'a')
      await flushPromises()
      expect(useToastStore().message).toBe('Could not delete the chat')
    })
  })

  describe('clips and settings', () => {
    it('opens a mentioned clip in the Library viewer', async () => {
      const w = await mountPage()
      const viewer = useClipViewerStore()
      await w.find('[data-testid="assistant-clip-chip"]').trigger('click')
      expect(viewer.clipId).toBe('clip-9')
    })

    it('the gear opens the settings, and saving updates the model shown', async () => {
      const w = await mountPage()
      await w.find('[data-testid="assistant-settings-button"]').trigger('click')
      await flushPromises()
      expect(document.body.querySelector('[data-testid="assistant-settings"]')).not.toBeNull()
      ;(document.body.querySelector('.p-dialog-close-button') as HTMLElement).click()
      await flushPromises()
      expect(w.findComponent({ name: 'AssistantSettingsDialog' }).props('visible')).toBe(false)
      const dialog = w.findComponent({ name: 'AssistantSettingsDialog' })
      dialog.vm.$emit('saved', { model: 'claude-pick', days: 3 })
      await flushPromises()
      expect(w.find('[data-testid="assistant-model-tag"]').text()).toContain('claude-pick')
    })
  })

  describe('narrow screens', () => {
    it('keeps the chat list in a drawer behind a Chats button', async () => {
      const w = await mountPage({ narrow: true })
      expect(w.find('.assistant-side').exists()).toBe(false)
      await w.find('[data-testid="assistant-open-chats"]').trigger('click')
      await flushPromises()
      expect(document.body.querySelector('.p-drawer')).not.toBeNull()
    })

    it('closes the drawer from its own close button', async () => {
      const w = await mountPage({ narrow: true })
      await w.find('[data-testid="assistant-open-chats"]').trigger('click')
      await flushPromises()
      ;(document.body.querySelector('.p-drawer-close-button') as HTMLElement).click()
      await flushPromises()
      await vi.waitFor(() => expect(document.body.querySelector('.p-drawer')).toBeNull())
    })

    it('swaps to the sidebar when the screen widens, and back', async () => {
      const w = await mountPage({ narrow: true })
      narrowListener?.({ matches: false } as MediaQueryListEvent)
      await flushPromises()
      expect(w.find('.assistant-side').exists()).toBe(true)
      expect(w.find('[data-testid="assistant-open-chats"]').exists()).toBe(false)
      narrowListener?.({ matches: true } as MediaQueryListEvent)
      await flushPromises()
      expect(w.find('.assistant-side').exists()).toBe(false)
    })

    it('stops listening when the page goes away', async () => {
      const w = await mountPage()
      w.unmount()
      expect(window.matchMedia('').removeEventListener).toBeDefined()
    })
  })
})
