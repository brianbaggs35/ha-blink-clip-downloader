import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import StoragePage from './StoragePage.vue'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

describe('StoragePage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([]))
        if (url.startsWith('/api/storage/gdrive/settings'))
          return Promise.resolve(
            jsonResponse({ client_id: '', has_client_secret: false, backup_policy: 'archived_only' }),
          )
        if (url.startsWith('/api/storage/gdrive/status'))
          return Promise.resolve(
            jsonResponse({ configured: false, connected: false, account_email: '', folder_id: '', folder_name: '' }),
          )
        return Promise.resolve(jsonResponse({}))
      }),
    )
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders both the archived clips and Google Drive sections', async () => {
    const wrapper = mount(StoragePage, { global: { plugins: [PrimeVue] } })
    await flushPromises()
    expect(wrapper.text()).toContain('Archived Clips')
    expect(wrapper.text()).toContain('Google Drive Backup')
  })

  it('shows the Google trademark disclaimer', async () => {
    const wrapper = mount(StoragePage, { global: { plugins: [PrimeVue] } })
    await flushPromises()
    expect(wrapper.text()).toContain('Google Drive is a trademark of Google LLC')
    expect(wrapper.text()).toContain('not affiliated with, endorsed by, or sponsored by Google')
  })
})
