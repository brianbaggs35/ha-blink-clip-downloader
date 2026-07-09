import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useDateFilterStore } from './dateFilter'

describe('useDateFilterStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('requestDate() sets the date and bumps seq', () => {
    const store = useDateFilterStore()
    expect(store.date).toBeNull()
    expect(store.seq).toBe(0)
    store.requestDate('2026-01-05')
    expect(store.date).toBe('2026-01-05')
    expect(store.seq).toBe(1)
  })

  it('requestDate() with the same date still bumps seq to re-trigger watchers', () => {
    const store = useDateFilterStore()
    store.requestDate('2026-01-05')
    store.requestDate('2026-01-05')
    expect(store.seq).toBe(2)
    expect(store.date).toBe('2026-01-05')
  })
})
