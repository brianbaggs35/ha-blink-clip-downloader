import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useRefreshStore } from './refresh'

describe('useRefreshStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('bump() increments tick so watchers can react', () => {
    const store = useRefreshStore()
    expect(store.tick).toBe(0)
    store.bump()
    store.bump()
    expect(store.tick).toBe(2)
  })
})
