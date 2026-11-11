import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  askAssistant,
  createAssistantChat,
  deleteAssistantChat,
  getAssistantChat,
  getAssistantModels,
  getAssistantStatus,
  listAssistantChats,
  renameAssistantChat,
  saveAssistantSettings,
} from './assistant'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('assistant api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getAssistantStatus()', async () => {
    await getAssistantStatus()
    expect(fetch).toHaveBeenCalledWith('/api/assistant/status', {})
  })

  it('getAssistantModels()', async () => {
    await getAssistantModels()
    expect(fetch).toHaveBeenCalledWith('/api/assistant/models', {})
  })

  it('saveAssistantSettings() PUTs the model and window', async () => {
    await saveAssistantSettings({ model: 'claude-x', days: 3 })
    expect(fetch).toHaveBeenCalledWith('/api/assistant/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'claude-x', days: 3 }),
    })
  })

  it('listAssistantChats()', async () => {
    await listAssistantChats()
    expect(fetch).toHaveBeenCalledWith('/api/assistant/chats', {})
  })

  it('createAssistantChat() sends a title only when given one', async () => {
    await createAssistantChat()
    expect(fetch).toHaveBeenLastCalledWith('/api/assistant/chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    await createAssistantChat('Porch')
    expect(fetch).toHaveBeenLastCalledWith('/api/assistant/chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'Porch' }),
    })
  })

  it('getAssistantChat() encodes the id', async () => {
    await getAssistantChat('a/b')
    expect(fetch).toHaveBeenCalledWith('/api/assistant/chats/a%2Fb', {})
  })

  it('renameAssistantChat() PATCHes the title', async () => {
    await renameAssistantChat('abc', 'New name')
    expect(fetch).toHaveBeenCalledWith('/api/assistant/chats/abc', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New name' }),
    })
  })

  it('deleteAssistantChat() DELETEs the chat', async () => {
    await deleteAssistantChat('abc')
    expect(fetch).toHaveBeenCalledWith('/api/assistant/chats/abc', { method: 'DELETE' })
  })

  it('askAssistant() POSTs the question to the chat', async () => {
    await askAssistant('abc', 'Anything today?')
    expect(fetch).toHaveBeenCalledWith('/api/assistant/chats/abc/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: 'Anything today?' }),
    })
  })
})
