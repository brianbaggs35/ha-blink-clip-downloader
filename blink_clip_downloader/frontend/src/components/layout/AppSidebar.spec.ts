import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AppSidebar from './AppSidebar.vue'
import { useThemeStore } from '../../stores/theme'
import { useConnectionStore } from '../../stores/connection'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown, ok = true) {
  return { ok, json: () => Promise.resolve(body), text: () => Promise.resolve('') } as Response
}

describe('AppSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('marks the active tab via data-tab/.active, matching the e2e smoke test contract', async () => {
    const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })

    const statusTab = wrapper.find('[data-tab="status"]')
    expect(statusTab.classes()).not.toContain('active')

    await statusTab.trigger('click')

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['status'])
  })

  it('emits help when the help button is clicked', async () => {
    const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
    await wrapper.find('[title="Keyboard shortcuts (?)"]').trigger('click')
    expect(wrapper.emitted('help')).toHaveLength(1)
  })

  it('theme button toggles the theme store', async () => {
    const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
    const theme = useThemeStore()
    expect(theme.isDark).toBe(true)
    await wrapper.find('[title="Toggle dark/light theme"]').trigger('click')
    expect(theme.isDark).toBe(false)
  })

  it('reflects the connection store across all three states', async () => {
    const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
    const connection = useConnectionStore()
    const badge = () => wrapper.find('.badge')

    expect(badge().text()).toBe('●')
    expect(badge().classes()).not.toContain('ok')
    expect(badge().classes()).not.toContain('err')

    connection.setConnected(true)
    await wrapper.vm.$nextTick()
    expect(badge().text()).toBe('● Connected')
    expect(badge().classes()).toContain('ok')

    connection.setConnected(false)
    await wrapper.vm.$nextTick()
    expect(badge().text()).toBe('● Disconnected')
    expect(badge().classes()).toContain('err')
  })

  it('refresh button bumps the refresh store and emits refresh', async () => {
    const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
    const refresh = useRefreshStore()
    await wrapper.find('.btn.sm.outline').trigger('click')
    expect(refresh.tick).toBe(1)
    expect(wrapper.emitted('refresh')).toHaveLength(1)
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
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      const refresh = useRefreshStore()
      const syncBtn = wrapper.findAll('.btn.sm')[1]

      await syncBtn.trigger('click')
      await vi.advanceTimersByTimeAsync(0)
      expect(syncBtn.attributes('disabled')).toBeDefined()
      expect(useToastStore().message).toContain('Download triggered')

      await vi.advanceTimersByTimeAsync(10000)
      expect(refresh.tick).toBe(1)

      await vi.advanceTimersByTimeAsync(3000)
      expect(wrapper.find('.btn.sm:last-child').attributes('disabled')).toBeUndefined()
    })

    it('toasts an error if the download-now request fails', async () => {
      vi.mocked(fetch).mockRejectedValue(new Error('network'))
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      const syncBtn = wrapper.findAll('.btn.sm')[1]

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
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      expect(wrapper.find('[title*="notification"]').exists()).toBe(false)
    })

    it('requests permission and enables notifications when granted', async () => {
      vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('granted') })
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      await wrapper.find('[title="Enable browser notifications"]').trigger('click')
      expect(localStorage.getItem('blink_notif')).toBe('1')
      expect(useToastStore().message).toContain('enabled')
    })

    it('shows a denial toast when permission is refused', async () => {
      vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('denied') })
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      await wrapper.find('[title="Enable browser notifications"]').trigger('click')
      expect(useToastStore().isError).toBe(true)
    })

    it('disables notifications on a second click', async () => {
      localStorage.setItem('blink_notif', '1')
      vi.stubGlobal('Notification', { requestPermission: vi.fn() })
      const wrapper = mount(AppSidebar, { props: { modelValue: 'library' } })
      await wrapper.find('[title="Notifications ON (click to disable)"]').trigger('click')
      expect(localStorage.getItem('blink_notif')).toBeNull()
      expect(useToastStore().message).toBe('Notifications disabled')
    })
  })
})
