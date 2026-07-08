import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AuthErrorBanner from './AuthErrorBanner.vue'
import { useAuthStore } from '../../stores/auth'

describe('AuthErrorBanner', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows the store message when visible', async () => {
    const wrapper = mount(AuthErrorBanner)
    const auth = useAuthStore()
    auth.bannerMessage = 'Blink authentication failed.'
    auth.bannerVisible = true
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).toContain('show')
    expect(wrapper.text()).toContain('Blink authentication failed.')
  })

  it('dismiss button calls dismissBanner()', async () => {
    const wrapper = mount(AuthErrorBanner)
    const auth = useAuthStore()
    auth.bannerMessage = 'Bad credentials'
    auth.bannerVisible = true
    await wrapper.vm.$nextTick()

    await wrapper.find('button').trigger('click')
    expect(auth.bannerVisible).toBe(false)
    expect(auth.dismissedMessage).toBe('Bad credentials')
  })
})
