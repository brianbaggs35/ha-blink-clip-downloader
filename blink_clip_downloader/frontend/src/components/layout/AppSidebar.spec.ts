import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import AppSidebar, { type TabName } from './AppSidebar.vue'
import { useThemeStore } from '../../stores/theme'
import { useConnectionStore } from '../../stores/connection'
import { useLibraryStore } from '../../stores/library'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import { useCapabilitiesStore } from '../../stores/capabilities'

function jsonResponse(body: unknown, ok = true) {
  return { ok, json: () => Promise.resolve(body), text: () => Promise.resolve('') } as Response
}

function mountSidebar(modelValue: TabName = 'library') {
  return mount(AppSidebar, {
    props: { modelValue },
    global: { plugins: [PrimeVue] },
  })
}

function findByText(wrapper: ReturnType<typeof mountSidebar>, text: string) {
  return wrapper.findAll('button').find((b) => b.text().includes(text))!
}

// PrimeVue's Dialog teleports to <body> by default (same pattern as
// ConfirmDialog.spec.ts) — assertions/interactions on the About dialog query
// body() rather than the mounted wrapper.
function body() {
  return new DOMWrapper(document.body)
}

describe('AppSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('marks the active tab via data-tab/.active, matching the e2e smoke test contract', async () => {
    const wrapper = mountSidebar('library')

    const statusTab = wrapper.find('[data-tab="status"]')
    expect(statusTab.classes()).not.toContain('active')

    await statusTab.trigger('click')

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['status'])
  })

  it('emits help when the help button is clicked', async () => {
    const wrapper = mountSidebar()
    await wrapper.find('[title="Keyboard shortcuts (?)"]').trigger('click')
    expect(wrapper.emitted('help')).toHaveLength(1)
  })

  it('theme button toggles the theme store', async () => {
    const wrapper = mountSidebar()
    const theme = useThemeStore()
    expect(theme.isDark).toBe(true)
    await wrapper.find('[title="Toggle dark/light theme"]').trigger('click')
    expect(theme.isDark).toBe(false)
  })

  it('reflects the connection store across all three states', async () => {
    const wrapper = mountSidebar()
    const connection = useConnectionStore()
    const tag = () => wrapper.findComponent({ name: 'Tag' })

    expect(tag().text()).toBe('Unknown')

    connection.setConnected(true)
    await wrapper.vm.$nextTick()
    expect(tag().text()).toBe('Connected')
    expect(tag().props('severity')).toBe('success')

    connection.setConnected(false)
    await wrapper.vm.$nextTick()
    expect(tag().text()).toBe('Disconnected')
    expect(tag().props('severity')).toBe('danger')
  })

  it('refresh button bumps the refresh store and emits refresh', async () => {
    const wrapper = mountSidebar()
    const refresh = useRefreshStore()
    await findByText(wrapper, 'Refresh').trigger('click')
    expect(refresh.tick).toBe(1)
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('shows the camera list on every tab once cameras have loaded, not just Library', async () => {
    const library = useLibraryStore()
    library.setCameras([{ camera: 'front', total: 3, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }])
    const wrapper = mountSidebar('library')
    expect(wrapper.find('[data-camera="front"]').exists()).toBe(true)
    expect(wrapper.find('[data-camera="all"]').exists()).toBe(true)

    // It's a persistent sidebar element (like Sync/Refresh/the connection
    // badge), not gated to the Library tab — switching away must not hide
    // it, or the camera activity counts are only ever visible on one tab.
    await wrapper.setProps({ modelValue: 'status' })
    expect(wrapper.find('[data-camera="front"]').exists()).toBe(true)
  })

  it('hides the camera list entirely until cameras have actually loaded', () => {
    const wrapper = mountSidebar('status')
    expect(wrapper.find('[data-camera="all"]').exists()).toBe(false)
  })

  it('shows 0 (not blank) for a camera with no clips yet', async () => {
    const library = useLibraryStore()
    library.setCameras([{ camera: 'front', total: 0, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }])
    const wrapper = mountSidebar('library')
    expect(wrapper.find('[data-camera="front"]').text()).toContain('0')
    expect(wrapper.find('[data-camera="all"]').text()).toContain('0')
  })

  it('selecting a camera updates the shared library store', async () => {
    const library = useLibraryStore()
    library.setCameras([{ camera: 'front', total: 3, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }])
    const wrapper = mountSidebar('library')
    await wrapper.find('[data-camera="front"]').trigger('click')
    expect(library.currentCamera).toBe('front')
  })

  it('selecting a camera from a non-Library tab switches to Library too', async () => {
    const library = useLibraryStore()
    library.setCameras([{ camera: 'front', total: 3, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }])
    const wrapper = mountSidebar('status')
    await wrapper.find('[data-camera="front"]').trigger('click')
    expect(library.currentCamera).toBe('front')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['library'])
  })

  describe('connection polling', () => {
    beforeEach(() => {
      vi.useFakeTimers()
    })
    afterEach(() => {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    })

    it('picks up the connection state on its own, without Library/Status ever mounting', async () => {
      // Regression test: the badge used to only update as a side effect of
      // Library's or Status's own /api/stats polling — if the app opened
      // straight to a different tab while Blink auth was still connecting,
      // the badge stayed stuck until the user happened to visit one of
      // those two tabs or reloaded the page.
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: true })))
      mountSidebar('ai')
      const connection = useConnectionStore()
      expect(connection.connected).toBe(null)

      await vi.advanceTimersByTimeAsync(0)
      expect(connection.connected).toBe(true)
    })

    it('re-polls periodically and reflects a state change with no manual trigger', async () => {
      const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ connected: false }))
      vi.stubGlobal('fetch', fetchMock)
      mountSidebar('ai')
      const connection = useConnectionStore()

      await vi.advanceTimersByTimeAsync(0)
      expect(connection.connected).toBe(false)

      fetchMock.mockResolvedValue(jsonResponse({ connected: true }))
      await vi.advanceTimersByTimeAsync(10000)
      expect(connection.connected).toBe(true)
    })

    it('leaves the badge at its last known state when a poll fails', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')))
      mountSidebar('ai')
      const connection = useConnectionStore()

      await vi.advanceTimersByTimeAsync(0)
      expect(connection.connected).toBe(null)
    })
  })

  describe('biometrics tab visibility', () => {
    beforeEach(() => {
      vi.useFakeTimers()
    })
    afterEach(() => {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    })

    it('shows Biometrics before the availability check resolves', () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: null })))
      const wrapper = mountSidebar()
      expect(wrapper.find('[data-tab="biometrics"]').exists()).toBe(true)
    })

    it('hides Biometrics once the CPU/dependency check reports unavailable', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: true, available: false, faces: [] })))
      const wrapper = mountSidebar()
      expect(wrapper.find('[data-tab="biometrics"]').exists()).toBe(true)

      await vi.advanceTimersByTimeAsync(0)
      await wrapper.vm.$nextTick()

      expect(wrapper.find('[data-tab="biometrics"]').exists()).toBe(false)
    })

    it('leaves Biometrics visible when face recognition is available', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: true, available: true, faces: [] })))
      const wrapper = mountSidebar()
      await vi.advanceTimersByTimeAsync(0)
      await wrapper.vm.$nextTick()

      expect(wrapper.find('[data-tab="biometrics"]').exists()).toBe(true)
    })

    it('leaves Biometrics visible when the availability check fails', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')))
      const wrapper = mountSidebar()
      await vi.advanceTimersByTimeAsync(0)
      await wrapper.vm.$nextTick()

      expect(wrapper.find('[data-tab="biometrics"]').exists()).toBe(true)
    })

    it('redirects away if the user is already on Biometrics when it becomes unavailable', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: true, available: false, faces: [] })))
      const wrapper = mountSidebar('biometrics')

      await vi.advanceTimersByTimeAsync(0)
      await wrapper.vm.$nextTick()

      expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['library'])
    })

    it('does not redirect away from Biometrics when it stays available', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ connected: true, available: true, faces: [] })))
      const wrapper = mountSidebar('biometrics')
      const capabilities = useCapabilitiesStore()

      await vi.advanceTimersByTimeAsync(0)
      await wrapper.vm.$nextTick()

      expect(capabilities.faceRecognitionAvailable).toBe(true)
      expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    })
  })

  describe('sync', () => {
    beforeEach(() => {
      vi.useFakeTimers()
      vi.stubGlobal('fetch', vi.fn())
    })
    afterEach(() => {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    })

    it('triggers a download, toasts, and bumps refresh after 10s', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
      const wrapper = mountSidebar()
      const refresh = useRefreshStore()
      const syncBtn = findByText(wrapper, 'Sync')

      await syncBtn.trigger('click')
      await vi.advanceTimersByTimeAsync(0)
      expect(useToastStore().message).toContain('Download triggered')

      await vi.advanceTimersByTimeAsync(10000)
      expect(refresh.tick).toBe(1)
    })

    it('toasts an error if the download-now request fails', async () => {
      vi.mocked(fetch).mockRejectedValue(new Error('network'))
      const wrapper = mountSidebar()
      const syncBtn = findByText(wrapper, 'Sync')

      await syncBtn.trigger('click')
      await vi.advanceTimersByTimeAsync(0)
      expect(useToastStore().isError).toBe(true)
      expect(useToastStore().message).toBe('Sync failed')
    })
  })

  describe('notifications', () => {
    afterEach(() => {
      vi.unstubAllGlobals()
    })

    it('hides the notification button when unsupported', () => {
      const wrapper = mountSidebar()
      expect(wrapper.find('[title*="notification"]').exists()).toBe(false)
    })

    it('requests permission and enables notifications when granted', async () => {
      vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('granted') })
      const wrapper = mountSidebar()
      await wrapper.find('[title="Enable browser notifications"]').trigger('click')
      expect(localStorage.getItem('blink_notif')).toBe('1')
      expect(useToastStore().message).toContain('enabled')
    })

    it('shows a denial toast when permission is refused', async () => {
      vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('denied') })
      const wrapper = mountSidebar()
      await wrapper.find('[title="Enable browser notifications"]').trigger('click')
      expect(useToastStore().isError).toBe(true)
    })

    it('disables notifications on a second click', async () => {
      localStorage.setItem('blink_notif', '1')
      vi.stubGlobal('Notification', { requestPermission: vi.fn() })
      const wrapper = mountSidebar()
      await wrapper.find('[title="Notifications ON (click to disable)"]').trigger('click')
      expect(localStorage.getItem('blink_notif')).toBeNull()
      expect(useToastStore().message).toBe('Notifications disabled')
    })
  })

  describe('About dialog', () => {
    it('is not shown until the About button is clicked', () => {
      mountSidebar()
      expect(body().find('.p-dialog').exists()).toBe(false)
    })

    it('opens on click and shows the author, repo link, and blinkpy credit', async () => {
      const wrapper = mountSidebar()
      await wrapper.find('[title="About this app"]').trigger('click')

      expect(body().text()).toContain('About Blink Clips')
      expect(body().text()).toContain('Brian Baggs')

      const repoLink = body()
        .findAll('a')
        .find((a) => a.attributes('href') === 'https://github.com/brianbaggs35/ha-blink-clip-downloader')
      expect(repoLink).toBeTruthy()

      const blinkpyLink = body()
        .findAll('a')
        .find((a) => a.attributes('href') === 'https://github.com/fronzbot/blinkpy')
      expect(blinkpyLink).toBeTruthy()
      expect(blinkpyLink!.text()).toBe('blinkpy')
      expect(body().text()).toContain('Built on blinkpy')
    })

    it('closes when the dialog emits update:visible(false), e.g. via its close button', async () => {
      const wrapper = mountSidebar()
      await wrapper.find('[title="About this app"]').trigger('click')
      const dialog = wrapper.findComponent({ name: 'Dialog' })
      expect(dialog.props('visible')).toBe(true)

      await dialog.vm.$emit('update:visible', false)
      expect(dialog.props('visible')).toBe(false)
    })
  })
})
