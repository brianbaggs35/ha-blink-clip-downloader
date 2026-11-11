import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import AssistantChatList from './AssistantChatList.vue'
import type { AssistantChatSummary } from '../../api/types'

const CHATS: AssistantChatSummary[] = [
  {
    id: 'a',
    title: 'Overnight',
    created_at: '2026-11-10T00:00:00Z',
    updated_at: '2026-11-10T00:00:00Z',
    message_count: 2,
  },
  {
    id: 'b',
    title: 'Batteries',
    created_at: '2026-11-09T00:00:00Z',
    updated_at: '2026-11-09T00:00:00Z',
    message_count: 4,
  },
]

function mountList(props: Partial<{ chats: AssistantChatSummary[]; activeId: string | null; busy: boolean }> = {}) {
  return mount(AssistantChatList, {
    props: { chats: CHATS, activeId: 'a', ...props },
    attachTo: document.body,
  })
}

/** Opens the item menu for a chat and clicks the named entry. */
async function chooseFromMenu(w: ReturnType<typeof mountList>, index: number, label: string) {
  await w.findAll('[data-testid="assistant-chat-options"]')[index].trigger('click')
  await nextTick()
  const item = [...document.body.querySelectorAll('.p-menu-item-link')].find((el) => el.textContent?.includes(label))
  expect(item).toBeTruthy()
  ;(item as HTMLElement).click()
  await nextTick()
}

describe('AssistantChatList', () => {
  it('lists the chats and marks the open one', () => {
    const w = mountList()
    const items = w.findAll('[data-testid="assistant-chat-item"]')
    expect(items.map((i) => i.text())).toEqual([
      expect.stringContaining('Overnight'),
      expect.stringContaining('Batteries'),
    ])
    expect(items[0].attributes('aria-current')).toBe('true')
    expect(items[1].attributes('aria-current')).toBeUndefined()
  })

  it('says so when there are no chats', () => {
    const w = mountList({ chats: [], activeId: null })
    expect(w.text()).toContain('No chats yet')
  })

  it('emits select and create', async () => {
    const w = mountList()
    await w.findAll('[data-testid="assistant-chat-item"]')[1].trigger('click')
    expect(w.emitted('select')).toEqual([['b']])
    await w.find('[data-testid="assistant-new-chat"]').trigger('click')
    expect(w.emitted('create')).toHaveLength(1)
  })

  it('disables new and options while an answer is pending', () => {
    const w = mountList({ busy: true })
    expect(w.find('[data-testid="assistant-new-chat"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="assistant-chat-options"]').attributes('disabled')).toBeDefined()
  })

  it('renames through the menu: Enter saves the trimmed title', async () => {
    const w = mountList()
    await chooseFromMenu(w, 1, 'Rename')
    const input = w.find('[data-testid="assistant-rename-input"]')
    expect((input.element as HTMLInputElement).value).toBe('Batteries')
    await input.setValue('  Cameras  ')
    await input.trigger('keydown', { key: 'Enter' })
    expect(w.emitted('rename')).toEqual([['b', 'Cameras']])
    expect(w.find('[data-testid="assistant-rename-input"]').exists()).toBe(false)
  })

  it('saves a rename when the field loses focus', async () => {
    const w = mountList()
    await chooseFromMenu(w, 0, 'Rename')
    const input = w.find('[data-testid="assistant-rename-input"]')
    await input.setValue('Last night')
    await input.trigger('blur')
    expect(w.emitted('rename')).toEqual([['a', 'Last night']])
  })

  it('Escape throws the edit away, and the blur that follows does not save it', async () => {
    const w = mountList()
    await chooseFromMenu(w, 0, 'Rename')
    const input = w.find('[data-testid="assistant-rename-input"]')
    await input.setValue('Something else')
    await input.trigger('keydown', { key: 'Escape' })
    await input.trigger('blur')
    expect(w.emitted('rename')).toBeUndefined()
    expect(w.find('[data-testid="assistant-rename-input"]').exists()).toBe(false)
  })

  it('does not emit a rename for an empty or unchanged title', async () => {
    const w = mountList()
    await chooseFromMenu(w, 0, 'Rename')
    await w.find('[data-testid="assistant-rename-input"]').setValue('   ')
    await w.find('[data-testid="assistant-rename-input"]').trigger('keydown', { key: 'Enter' })
    await chooseFromMenu(w, 0, 'Rename')
    await w.find('[data-testid="assistant-rename-input"]').trigger('keydown', { key: 'Enter' })
    expect(w.emitted('rename')).toBeUndefined()
  })

  it('a second blur after committing does nothing', async () => {
    const w = mountList()
    await chooseFromMenu(w, 0, 'Rename')
    const input = w.find('[data-testid="assistant-rename-input"]')
    await input.setValue('Renamed')
    await input.trigger('keydown', { key: 'Enter' })
    await input.trigger('blur')
    expect(w.emitted('rename')).toHaveLength(1)
  })

  it('asks to remove the chat chosen from the menu', async () => {
    const w = mountList()
    await chooseFromMenu(w, 1, 'Delete')
    expect(w.emitted('remove')).toEqual([['b']])
  })
})
