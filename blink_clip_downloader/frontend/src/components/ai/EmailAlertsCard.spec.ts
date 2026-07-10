import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import EmailAlertsCard from './EmailAlertsCard.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

describe('EmailAlertsCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows the not-configured message when SMTP is not set up', () => {
    const wrapper = mount(EmailAlertsCard, { props: { smtpConfigured: false } })
    expect(wrapper.text()).toContain('No SMTP settings configured')
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('sends a test email and shows a success result', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ success: true, message: 'Sent to you@example.com' }))),
    )
    const wrapper = mount(EmailAlertsCard, { props: { smtpConfigured: true } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('✓ Sent to you@example.com')
  })

  it('shows a failure result when the send reports failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ success: false, message: 'SMTP auth failed' }))),
    )
    const wrapper = mount(EmailAlertsCard, { props: { smtpConfigured: true } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('✗ SMTP auth failed')
  })

  it('shows a generic failure message when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const wrapper = mount(EmailAlertsCard, { props: { smtpConfigured: true } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('check the add-on logs')
  })
})
