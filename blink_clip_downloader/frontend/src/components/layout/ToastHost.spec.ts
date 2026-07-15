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

  it('puts the message in the summary slot, not detail, so no empty line pushes the icon out of line', async () => {
    // ToastMessage.vue always renders the summary <span> unconditionally
    // but only renders detail with v-if="message.detail" — using detail
    // for our one-line messages left a blank (but still laid-out) summary
    // line above the text, which is what actually caused the checkmark
    // icon to look vertically misaligned with the message.
    mountHost()
    const toast = useToastStore()
    toast.show('Camera configs saved')
    await flush()
    // Neither this test nor its neighbors unmount their host, so PrimeVue's
    // ToastService can carry earlier tests' still-life toasts in the DOM —
    // .at(-1) targets the one this test just added, not whichever is first.
    const summaries = body().findAll('.p-toast-summary')
    expect(summaries.at(-1)!.text()).toBe('Camera configs saved')
    expect(body().findAll('.p-toast-detail').length).toBe(0)
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
