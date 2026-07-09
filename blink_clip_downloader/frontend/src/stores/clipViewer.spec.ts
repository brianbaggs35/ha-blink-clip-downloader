import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useClipViewerStore } from './clipViewer'

describe('useClipViewerStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('requestOpen() sets the clip id and bumps seq', () => {
    const store = useClipViewerStore()
    store.requestOpen('c1')
    expect(store.clipId).toBe('c1')
    expect(store.seq).toBe(1)
  })

  it('requesting the same clip again still bumps seq to re-trigger watchers', () => {
    const store = useClipViewerStore()
    store.requestOpen('c1')
    store.requestOpen('c1')
    expect(store.seq).toBe(2)
  })
})
