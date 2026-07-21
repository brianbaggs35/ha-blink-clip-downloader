import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import TwoFAOverlay from './TwoFAOverlay.vue'
import { useAuthStore } from '../../stores/auth'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

describe('TwoFAOverlay', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens when the auth store needs 2FA', async () => {
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).toContain('open')
  })

  it('closes when the auth store no longer needs 2FA', async () => {
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).toContain('open')

    auth.state = 'connected'
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).not.toContain('open')
  })

  it('submits the entered code via the auth store', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ seq: 1 }))
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()

    await wrapper.find('input').setValue('123456')
    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      '/api/auth/2fa',
      expect.objectContaining({ body: JSON.stringify({ code: '123456' }) }),
    )
    expect(wrapper.find('button').text()).toContain('Submitted')
  })

  it('shows the verifying label while the request is in flight', async () => {
    let resolveFetch: (v: Response) => void = () => {}
    vi.mocked(fetch).mockReturnValue(new Promise((resolve) => (resolveFetch = resolve)))
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()

    await wrapper.find('input').setValue('123456')
    const clickPromise = wrapper.find('button').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('button').text()).toContain('Verifying')

    resolveFetch(jsonResponse({ seq: 1 }))
    await clickPromise
  })

  it('clears the input and refocuses after a rejected code', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse('Invalid code', false))
    const wrapper = mount(TwoFAOverlay, { attachTo: document.body })
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()

    const input = wrapper.find('input')
    await input.setValue('123456')
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect((input.element as HTMLInputElement).value).toBe('')
    expect(document.activeElement).toBe(input.element)
    wrapper.unmount()
  })

  it('non-digit keys other than control keys are prevented', async () => {
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()

    const input = wrapper.find('input')
    const event = new KeyboardEvent('keydown', { key: 'a', cancelable: true })
    input.element.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  })

  it('Enter key submits the form', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ seq: 1 }))
    const wrapper = mount(TwoFAOverlay)
    const auth = useAuthStore()
    auth.state = 'needs_2fa'
    await wrapper.vm.$nextTick()
    await wrapper.find('input').setValue('123456')

    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true })
    wrapper.find('input').element.dispatchEvent(event)
    await flushPromises()
    expect(fetch).toHaveBeenCalled()
  })
})

function flushPromises() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}
