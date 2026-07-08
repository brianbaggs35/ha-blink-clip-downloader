import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { useKeyboardShortcuts } from './useKeyboardShortcuts'
import { useConfirmStore } from '../stores/confirm'

function mountHost(helpOpen = ref(false)) {
  const Host = defineComponent({
    setup() {
      useKeyboardShortcuts(helpOpen)
      return () => null
    },
  })
  return { wrapper: mount(Host), helpOpen }
}

function dispatchKey(key: string, target: EventTarget = document.body) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  Object.defineProperty(event, 'target', { value: target })
  document.dispatchEvent(event)
}

describe('useKeyboardShortcuts', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('"?" toggles help open/closed', () => {
    const { helpOpen } = mountHost()
    dispatchKey('?')
    expect(helpOpen.value).toBe(true)
    dispatchKey('?')
    expect(helpOpen.value).toBe(false)
  })

  it('Escape closes help first if it is open', () => {
    const { helpOpen } = mountHost(ref(true))
    dispatchKey('Escape')
    expect(helpOpen.value).toBe(false)
  })

  it('Escape closes the confirm dialog when help is not open', () => {
    mountHost(ref(false))
    const confirm = useConfirmStore()
    void confirm.ask('Sure?')
    expect(confirm.open).toBe(true)
    dispatchKey('Escape')
    expect(confirm.open).toBe(false)
  })

  it('Escape does nothing when neither help nor confirm is open', () => {
    const { helpOpen } = mountHost(ref(false))
    const confirm = useConfirmStore()
    expect(() => dispatchKey('Escape')).not.toThrow()
    expect(helpOpen.value).toBe(false)
    expect(confirm.open).toBe(false)
  })

  it('ignores keydowns originating from an <input>', () => {
    const { helpOpen } = mountHost()
    const input = document.createElement('input')
    dispatchKey('?', input)
    expect(helpOpen.value).toBe(false)
  })

  it('removes its listener on unmount', () => {
    const helpOpen = ref(false)
    const Host = defineComponent({
      setup() {
        useKeyboardShortcuts(helpOpen)
        return () => null
      },
    })
    const wrapper = mount(Host)
    wrapper.unmount()
    dispatchKey('?')
    expect(helpOpen.value).toBe(false)
  })
})
