import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { usePromptOverlayStore } from './promptOverlay'

describe('usePromptOverlayStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('show() sets the prompt text and opens the overlay', () => {
    const store = usePromptOverlayStore()
    store.show('the actual prompt')
    expect(store.open).toBe(true)
    expect(store.promptText).toBe('the actual prompt')
  })

  it('show() falls back to a placeholder when no prompt was captured', () => {
    const store = usePromptOverlayStore()
    store.show('')
    expect(store.promptText).toBe('No prompt was captured for this clip.')
  })

  it('close() closes the overlay without clearing the text', () => {
    const store = usePromptOverlayStore()
    store.show('x')
    store.close()
    expect(store.open).toBe(false)
    expect(store.promptText).toBe('x')
  })
})
