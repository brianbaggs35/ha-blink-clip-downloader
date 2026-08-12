import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import GoogleDriveCard from './GoogleDriveCard.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const NOT_CONFIGURED = { client_id: '', has_client_secret: false, backup_policy: 'archived_only' }
const CONFIGURED = { client_id: 'cid', has_client_secret: true, backup_policy: 'archived_only' }
const NOT_CONNECTED_STATUS = {
  configured: true,
  connected: false,
  account_email: '',
  folder_id: '',
  folder_name: '',
}
const CONNECTED_NO_FOLDER = {
  configured: true,
  connected: true,
  account_email: 'me@example.com',
  folder_id: '',
  folder_name: '',
}
const CONNECTED_WITH_FOLDER = {
  configured: true,
  connected: true,
  account_email: 'me@example.com',
  folder_id: 'f1',
  folder_name: 'Blink Clips',
}
const IDLE_CONNECT = { phase: 'idle' }

interface Routes {
  settings?: unknown
  status?: unknown
  quota?: unknown
  queue?: unknown
  connectStatus?: unknown
  folders?: unknown
  failedUploads?: unknown
}

function routedFetch(routes: Routes) {
  return vi.fn((url: string, opts?: RequestInit) => {
    const method = opts?.method
    if (url === '/api/storage/gdrive/settings' && method === 'PUT')
      return Promise.resolve(jsonResponse({ saved: true }))
    if (url.startsWith('/api/storage/gdrive/settings')) return Promise.resolve(jsonResponse(routes.settings))
    if (url === '/api/storage/gdrive/connect' && method === 'POST')
      return Promise.resolve(
        jsonResponse({
          phase: 'pending',
          user_code: 'ABCD-1234',
          verification_url: 'https://google.com/device',
          expires_in: 1800,
        }),
      )
    if (url.startsWith('/api/storage/gdrive/connect-status'))
      return Promise.resolve(jsonResponse(routes.connectStatus ?? IDLE_CONNECT))
    if (url === '/api/storage/gdrive/disconnect') return Promise.resolve(jsonResponse({ disconnected: true }))
    if (url === '/api/storage/gdrive/backup-now') return Promise.resolve(jsonResponse({ enqueued: 3 }))
    if (url === '/api/storage/gdrive/retry' && method === 'POST') return Promise.resolve(jsonResponse({ retried: 1 }))
    if (url === '/api/storage/gdrive/folder' && method === 'PUT') return Promise.resolve(jsonResponse({ saved: true }))
    if (url.startsWith('/api/storage/gdrive/status')) return Promise.resolve(jsonResponse(routes.status))
    if (url.startsWith('/api/storage/gdrive/quota'))
      return Promise.resolve(jsonResponse(routes.quota ?? { available: false }))
    // Must come before the /api/storage/gdrive/queue prefix check below —
    // that check's startsWith would otherwise also swallow this route.
    if (url.startsWith('/api/storage/gdrive/queue/failed'))
      return Promise.resolve(jsonResponse(routes.failedUploads ?? []))
    if (url.startsWith('/api/storage/gdrive/queue'))
      return Promise.resolve(
        jsonResponse(routes.queue ?? { connected: false, pending: 0, processing: 0, completed: 0, failed: 0 }),
      )
    if (url.startsWith('/api/storage/gdrive/folders'))
      return Promise.resolve(jsonResponse(routes.folders ?? { folders: [] }))
    return Promise.resolve(jsonResponse({}))
  })
}

function mountCard() {
  return mount(GoogleDriveCard, { global: { plugins: [PrimeVue] } })
}

describe('GoogleDriveCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows a message when not configured', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        settings: NOT_CONFIGURED,
        status: { configured: false, connected: false, account_email: '', folder_id: '', folder_name: '' },
      }),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Set a Google OAuth Client ID and Secret above')
  })

  it('saves settings from the setup form', async () => {
    const fetchMock = routedFetch({ settings: NOT_CONFIGURED, status: NOT_CONNECTED_STATUS })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    await wrapper.find('#gdrive-client-id').setValue('new-client-id')
    await wrapper.find('#gdrive-client-secret input').setValue('new-secret')
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save Setup')
    await saveBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: 'new-client-id', client_secret: 'new-secret', backup_policy: 'archived_only' }),
    })
  })

  it('shows a Connect button once configured but not connected', async () => {
    vi.stubGlobal('fetch', routedFetch({ settings: CONFIGURED, status: NOT_CONNECTED_STATUS }))
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Connect Google Drive')
  })

  it('starting a connection shows the user code and verification URL, and polls for completion', async () => {
    vi.useFakeTimers()
    const routes: Routes = { settings: CONFIGURED, status: NOT_CONNECTED_STATUS }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    const connectBtn = wrapper.findAll('button').find((b) => b.text() === 'Connect Google Drive')
    await connectBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('ABCD-1234')
    expect(wrapper.text()).toContain('https://google.com/device')

    // Next poll reports connected — the card should refresh status and show the connected view.
    routes.connectStatus = { phase: 'connected', account_email: 'me@example.com' }
    routes.status = CONNECTED_WITH_FOLDER
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(wrapper.text()).toContain('me@example.com')
  })

  it('resumes polling on mount if a connect was already in flight', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      settings: CONFIGURED,
      status: NOT_CONNECTED_STATUS,
      connectStatus: { phase: 'pending', user_code: 'EXISTING-CODE', verification_url: 'https://google.com/device' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.text()).toContain('EXISTING-CODE')
  })

  it('shows an error message once the poll reports the sign-in was denied', async () => {
    vi.useFakeTimers()
    const routes: Routes = { settings: CONFIGURED, status: NOT_CONNECTED_STATUS }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountCard()
    await flushPromises()

    const connectBtn = wrapper.findAll('button').find((b) => b.text() === 'Connect Google Drive')
    await connectBtn!.trigger('click')
    await flushPromises()

    routes.connectStatus = { phase: 'error', message: 'Sign-in was denied' }
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(wrapper.text()).toContain('Sign-in was denied')
  })

  it('shows an expired message once the poll reports the device code timed out', async () => {
    vi.useFakeTimers()
    const routes: Routes = { settings: CONFIGURED, status: NOT_CONNECTED_STATUS }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountCard()
    await flushPromises()

    const connectBtn = wrapper.findAll('button').find((b) => b.text() === 'Connect Google Drive')
    await connectBtn!.trigger('click')
    await flushPromises()

    routes.connectStatus = { phase: 'expired' }
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(wrapper.text()).toContain('Sign-in code expired')
  })

  it('shows the folder browser inline when connected with no folder chosen yet', async () => {
    vi.stubGlobal('fetch', routedFetch({ settings: CONFIGURED, status: CONNECTED_NO_FOLDER }))
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Choose a folder for your backups')
  })

  it('selecting a folder inline saves it and reloads status', async () => {
    const routes: Routes = {
      settings: CONFIGURED,
      status: CONNECTED_NO_FOLDER,
      folders: { folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] },
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    routes.status = CONNECTED_WITH_FOLDER
    const selectBtn = wrapper.findAll('button').find((b) => b.text() === 'Select')
    await selectBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/folder', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: 'f1', folder_name: 'Blink Clips' }),
    })
    expect(wrapper.text()).toContain('Blink Clips')
  })

  it('shows account, folder, quota, and queue status once fully connected', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        settings: CONFIGURED,
        status: CONNECTED_WITH_FOLDER,
        quota: { available: true, limit: 1_073_741_824 * 10, usage: 1_073_741_824 * 2, usage_in_drive: 1_073_741_824 },
        queue: { connected: true, pending: 2, processing: 1, completed: 5, failed: 0 },
      }),
    )
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.text()).toContain('me@example.com')
    expect(wrapper.text()).toContain('Blink Clips')
    expect(wrapper.text()).toContain('2 pending')
    expect(wrapper.text()).toContain('GB used')
  })

  it('shows "Unlimited storage" when the quota has no limit', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        settings: CONFIGURED,
        status: CONNECTED_WITH_FOLDER,
        quota: { available: true, limit: null, usage: 500, usage_in_drive: 500 },
      }),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Unlimited storage')
  })

  it('treats a failed quota fetch as unavailable rather than crashing', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url.startsWith('/api/storage/gdrive/quota'))
        return Promise.resolve({ ok: false, status: 500, statusText: 'err', text: () => Promise.resolve('') })
      return routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER })(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.text()).toContain('me@example.com')
    expect(wrapper.text()).not.toContain('Unlimited storage')
    expect(wrapper.text()).not.toContain('GB used')
  })

  it('shows an error toast when triggering a manual backup fails', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/gdrive/backup-now')
        return Promise.resolve({ ok: false, status: 500, statusText: 'err', text: () => Promise.resolve('') })
      return routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER })(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    const backupBtn = wrapper.findAll('button').find((b) => b.text() === 'Back Up Existing Clips Now')
    await backupBtn!.trigger('click')
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Could not start backup')
    expect(toast.isError).toBe(true)
  })

  it('triggers a manual backup and shows the enqueued count', async () => {
    const fetchMock = routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    const backupBtn = wrapper.findAll('button').find((b) => b.text() === 'Back Up Existing Clips Now')
    await backupBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/backup-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('disconnects after confirmation', async () => {
    const fetchMock = routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    const confirm = useConfirmStore()
    const disconnectBtn = wrapper.findAll('button').find((b) => b.text() === 'Disconnect')
    const clickPromise = disconnectBtn!.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/disconnect', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('does not disconnect when declined', async () => {
    const fetchMock = routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    const confirm = useConfirmStore()
    const disconnectBtn = wrapper.findAll('button').find((b) => b.text() === 'Disconnect')
    const clickPromise = disconnectBtn!.trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalledWith('/api/storage/gdrive/disconnect', expect.anything())
  })

  it('shows failed uploads and reloads queue status after a retry', async () => {
    const routes: Routes = {
      settings: CONFIGURED,
      status: CONNECTED_WITH_FOLDER,
      queue: { connected: true, pending: 0, processing: 0, completed: 5, failed: 1 },
      failedUploads: [
        {
          clip_id: 'c1',
          camera: 'Front Door',
          clip_path: '/c1.mp4',
          error_message: 'quota exceeded',
          completed_at: 't',
        },
      ],
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed Uploads (1)')
    expect(wrapper.text()).toContain('quota exceeded')

    routes.queue = { connected: true, pending: 1, processing: 0, completed: 5, failed: 0 }
    const retryBtn = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_id: 'c1' }),
    })
    expect(wrapper.text()).toContain('1 pending')
  })

  it('does not show the failed uploads section when nothing has failed', async () => {
    vi.stubGlobal('fetch', routedFetch({ settings: CONFIGURED, status: CONNECTED_WITH_FOLDER, failedUploads: [] }))
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Failed Uploads')
  })

  it('opens the Change Folder dialog and updates the folder on selection', async () => {
    const routes: Routes = {
      settings: CONFIGURED,
      status: CONNECTED_WITH_FOLDER,
      folders: { folders: [{ id: 'f2', name: 'Other Folder', modified_time: '' }] },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountCard()
    await flushPromises()

    const changeBtn = wrapper.findAll('button').find((b) => b.text() === 'Change Folder')
    await changeBtn!.trigger('click')
    await flushPromises()

    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Other Folder')
  })
})
