import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import AppSidebar, { type TabName } from './AppSidebar.vue'
import { useThemeStore } from '../../stores/theme'
import { useConnectionStore } from '../../stores/connection'
import { useLibraryStore } from '../../stores/library'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

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

  it('shows the camera list only when the Library tab is active', async () => {
    const library = useLibraryStore()
    library.setCameras([{ camera: 'front', total: 3, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }])
    const wrapper = mountSidebar('library')
    expect(wrapper.find('[data-camera="front"]').exists()).toBe(true)
    expect(wrapper.find('[data-camera="all"]').exists()).toBe(true)

    await wrapper.setProps({ modelValue: 'status' })
    expect(wrapper.find('[data-camera="front"]').exists()).toBe(false)
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
})
