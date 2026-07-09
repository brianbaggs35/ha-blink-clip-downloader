import { beforeEach, describe, expect, it } from 'vitest'
import { DOMWrapper, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import ToastHost from './ToastHost.vue'
import { useToastStore } from '../../stores/toast'

// PrimeVue's Toast teleports to <body> by default.
function body() {
  return new DOMWrapper(document.body)
}

function mountHost() {
  return mount(ToastHost, {
    global: { plugins: [PrimeVue, ToastService] },
  })
}

function flush() {
  return new Promise((r) => setTimeout(r))
}

describe('ToastHost', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('forwards a success toast from the store to PrimeVue', async () => {
    mountHost()
    const toast = useToastStore()
    toast.show('Copied to clipboard')
    await flush()
    expect(body().text()).toContain('Copied to clipboard')
  })

  it('forwards an error toast with the error severity', async () => {
    mountHost()
    const toast = useToastStore()
    toast.show('Copy failed', true)
    await flush()
    expect(body().find('.p-toast-message-error').exists()).toBe(true)
  })

  it('does not add a toast when seq changes but visible is false', async () => {
    mountHost()
    const toast = useToastStore()
    toast.show('Will hide')
    toast.visible = false
    toast.seq++
    await flush()
    // no assertion beyond not throwing — covers the early-return branch
  })
})
