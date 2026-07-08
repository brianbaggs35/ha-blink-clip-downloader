import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useConnectionStore } from './connection'

describe('useConnectionStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts unknown (null) and can be set', () => {
    const store = useConnectionStore()
    expect(store.connected).toBeNull()
    store.setConnected(true)
    expect(store.connected).toBe(true)
    store.setConnected(false)
    expect(store.connected).toBe(false)
  })
})
