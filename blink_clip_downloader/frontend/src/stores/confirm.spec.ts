import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useConfirmStore } from './confirm'

describe('useConfirmStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('ask() opens the dialog and resolves true/false via settle()', async () => {
    const store = useConfirmStore()
    const promise = store.ask('Delete this clip?', 'Delete clip')
    expect(store.open).toBe(true)
    expect(store.title).toBe('Delete clip')
    expect(store.message).toBe('Delete this clip?')

    store.settle(true)
    await expect(promise).resolves.toBe(true)
    expect(store.open).toBe(false)
  })

  it('defaults the title when none is given', () => {
    const store = useConfirmStore()
    void store.ask('Are you sure you want to proceed?')
    expect(store.title).toBe('Are you sure?')
    store.settle(false)
  })

  it('settle() without a pending ask() is a no-op', () => {
    const store = useConfirmStore()
    expect(() => store.settle(false)).not.toThrow()
  })
})
