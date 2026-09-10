import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useNavCollapsedStore } from './navCollapsed'

describe('useNavCollapsedStore', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('defaults to expanded when nothing is stored', () => {
    expect(useNavCollapsedStore().collapsed).toBe(false)
  })

  it('respects a stored collapsed preference', () => {
    localStorage.setItem('blink_nav_collapsed', '1')
    setActivePinia(createPinia())
    expect(useNavCollapsedStore().collapsed).toBe(true)
  })

  it('toggle() flips state and persists the choice', () => {
    const store = useNavCollapsedStore()
    store.toggle()
    expect(store.collapsed).toBe(true)
    expect(localStorage.getItem('blink_nav_collapsed')).toBe('1')

    store.toggle()
    expect(store.collapsed).toBe(false)
    expect(localStorage.getItem('blink_nav_collapsed')).toBe('0')
  })
})
