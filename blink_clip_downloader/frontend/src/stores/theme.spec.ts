import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useThemeStore } from './theme'

describe('useThemeStore', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('defaults to dark when nothing is stored', () => {
    expect(useThemeStore().isDark).toBe(true)
  })

  it('respects a stored light preference', () => {
    localStorage.setItem('blink_theme', 'light')
    setActivePinia(createPinia())
    expect(useThemeStore().isDark).toBe(false)
  })

  it('toggle() flips state and persists the choice', () => {
    const store = useThemeStore()
    store.toggle()
    expect(store.isDark).toBe(false)
    expect(localStorage.getItem('blink_theme')).toBe('light')

    store.toggle()
    expect(store.isDark).toBe(true)
    expect(localStorage.getItem('blink_theme')).toBe('dark')
  })
})
