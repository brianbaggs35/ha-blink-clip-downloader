import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useConfirm } from './useConfirm'
import { useConfirmStore } from '../stores/confirm'

describe('useConfirm', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('delegates to the confirm store and resolves with its result', async () => {
    const confirm = useConfirm()
    const promise = confirm('Delete this clip?', 'Delete clip')
    const store = useConfirmStore()
    expect(store.open).toBe(true)
    store.settle(true)
    await expect(promise).resolves.toBe(true)
  })
})
