import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import NotificationChannelsCard from './NotificationChannelsCard.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function findButtonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes(text))
  if (!btn) throw new Error(`button containing "${text}" not found`)
  return btn
}

describe('NotificationChannelsCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders a test action for each channel', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ success: true, message: '' }))),
    )
    const wrapper = mount(NotificationChannelsCard)
    expect(wrapper.text()).toContain('Email')
    expect(wrapper.text()).toContain('Discord')
    expect(wrapper.text()).toContain('Mobile App')
    expect(wrapper.text()).toContain('Home Assistant')
    expect(wrapper.findAll('button')).toHaveLength(4)
  })

  it('sends a test email and shows the success message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        expect(url).toContain('/api/notifications/test-email')
        return Promise.resolve(jsonResponse({ success: true, message: 'Test email sent to you@example.com' }))
      }),
    )
    const wrapper = mount(NotificationChannelsCard)
    await findButtonByText(wrapper, 'Send test email').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Test email sent to you@example.com')
  })

  it('sends a test Discord message and shows the success message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        expect(url).toContain('/api/notifications/test-discord')
        return Promise.resolve(jsonResponse({ success: true, message: 'Test message sent to Discord.' }))
      }),
    )
    const wrapper = mount(NotificationChannelsCard)
    await findButtonByText(wrapper, 'Send test message').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Test message sent to Discord.')
  })

  it('sends a test mobile notification and shows a failure message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        expect(url).toContain('/api/notifications/test-mobile')
        return Promise.resolve(jsonResponse({ success: false, message: 'Mobile app target is not configured.' }))
      }),
    )
    const wrapper = mount(NotificationChannelsCard)
    await findButtonByText(wrapper, 'Send test notification').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Mobile app target is not configured.')
  })

  it('sends a test HA notification and shows the success message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        expect(url).toContain('/api/notifications/test-ha')
        return Promise.resolve(jsonResponse({ success: true, message: 'Test notification sent to Home Assistant.' }))
      }),
    )
    const wrapper = mount(NotificationChannelsCard)
    await findButtonByText(wrapper, 'Send test HA notification').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Test notification sent to Home Assistant.')
  })

  it('shows a generic failure message when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const wrapper = mount(NotificationChannelsCard)
    await findButtonByText(wrapper, 'Send test email').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('check the add-on logs')
  })
})
