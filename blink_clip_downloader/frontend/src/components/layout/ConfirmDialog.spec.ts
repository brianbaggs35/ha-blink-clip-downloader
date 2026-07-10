import { beforeEach, describe, expect, it } from 'vitest'
import { DOMWrapper, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmDialog from './ConfirmDialog.vue'
import { useConfirmStore } from '../../stores/confirm'

// PrimeVue's Dialog teleports to <body> by default, so assertions/interactions
// query body() rather than the mounted wrapper (matches the pattern already
// used for LibraryPage's Teleported ClipModal).
function body() {
  return new DOMWrapper(document.body)
}

function mountDialog() {
  return mount(ConfirmDialog, { global: { plugins: [PrimeVue] } })
}

describe('ConfirmDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('opens with the store title/message and resolves true on Confirm', async () => {
    mountDialog()
    const store = useConfirmStore()
    const promise = store.ask('Delete this clip?', 'Delete clip')
    await new Promise((r) => setTimeout(r))

    expect(body().text()).toContain('Delete clip')
    expect(body().text()).toContain('Delete this clip?')

    const confirmBtn = body()
      .findAll('button')
      .find((b) => b.text() === 'Confirm')!
    await confirmBtn.trigger('click')
    await expect(promise).resolves.toBe(true)
    expect(store.open).toBe(false)
  })

  it('Cancel resolves false', async () => {
    mountDialog()
    const store = useConfirmStore()
    const promise = store.ask('Sure?')
    await new Promise((r) => setTimeout(r))
    const cancelBtn = body()
      .findAll('button')
      .find((b) => b.text() === 'Cancel')!
    await cancelBtn.trigger('click')
    await expect(promise).resolves.toBe(false)
  })

  it("dismissing via the dialog's own close button resolves false", async () => {
    mountDialog()
    const store = useConfirmStore()
    const promise = store.ask('Sure?')
    await new Promise((r) => setTimeout(r))
    await body().find('.p-dialog-close-button').trigger('click')
    await expect(promise).resolves.toBe(false)
  })
})
