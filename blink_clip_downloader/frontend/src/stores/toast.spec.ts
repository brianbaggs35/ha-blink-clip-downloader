import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useToastStore } from './toast'

describe('useToastStore', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows a message and auto-hides after the default duration', () => {
    const toast = useToastStore()
    toast.show('Saved')
    expect(toast.visible).toBe(true)
    expect(toast.message).toBe('Saved')
    expect(toast.isError).toBe(false)

    vi.advanceTimersByTime(2799)
    expect(toast.visible).toBe(true)
    vi.advanceTimersByTime(1)
    expect(toast.visible).toBe(false)
  })

  it('supports an error toast with a custom duration', () => {
    const toast = useToastStore()
    toast.show('Failed', true, 500)
    expect(toast.isError).toBe(true)
    vi.advanceTimersByTime(500)
    expect(toast.visible).toBe(false)
  })

  it('re-triggering show() resets the hide timer', () => {
    const toast = useToastStore()
    toast.show('First')
    vi.advanceTimersByTime(2000)
    toast.show('Second')
    vi.advanceTimersByTime(2000)
    expect(toast.visible).toBe(true)
    expect(toast.message).toBe('Second')
  })

  it('bumps seq on every show() so repeated identical toasts are distinguishable', () => {
    const toast = useToastStore()
    toast.show('Same message')
    expect(toast.seq).toBe(1)
    toast.show('Same message')
    expect(toast.seq).toBe(2)
  })
})
