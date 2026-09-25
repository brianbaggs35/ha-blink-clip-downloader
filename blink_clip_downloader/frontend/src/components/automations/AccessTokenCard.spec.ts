import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AccessTokenCard from './AccessTokenCard.vue'
import { useAccessStore } from '../../stores/access'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

function mountCard(loginEnabled = true) {
  const access = useAccessStore()
  access.loginEnabled = loginEnabled
  access.accessToken = 'current-token'
  return mount(AccessTokenCard)
}

function tokenField(wrapper: ReturnType<typeof mountCard>) {
  return wrapper.find('#access-token').element as HTMLInputElement
}

describe('AccessTokenCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders nothing while Direct Access Sign-In is off', () => {
    const wrapper = mountCard(false)
    expect(wrapper.find('[data-testid="access-token-card"]').exists()).toBe(false)
  })

  it('keeps the token masked until asked, and hides it again', async () => {
    const wrapper = mountCard()
    expect(tokenField(wrapper).value).not.toContain('current-token')
    const toggle = wrapper.find('[data-testid="access-token-toggle"]')
    await toggle.trigger('click')
    expect(tokenField(wrapper).value).toBe('current-token')
    expect(toggle.text()).toBe('Hide')
    await toggle.trigger('click')
    expect(tokenField(wrapper).value).not.toContain('current-token')
  })

  it('regenerates after confirmation and shows the new token', async () => {
    const wrapper = mountCard()
    const access = useAccessStore()
    const regenerate = vi.spyOn(access, 'regenerateToken').mockImplementation(async () => {
      access.accessToken = 'new-token'
      return 'new-token'
    })
    vi.spyOn(useConfirmStore(), 'ask').mockResolvedValue(true)
    const show = vi.spyOn(useToastStore(), 'show')

    await wrapper.find('[data-testid="access-token-regenerate"]').trigger('click')
    await flushPromises()

    expect(regenerate).toHaveBeenCalled()
    expect(tokenField(wrapper).value).toBe('new-token')
    expect(show).toHaveBeenCalledWith('New access token created — copy the YAML below again')
  })

  it('does nothing when the confirmation is declined', async () => {
    const wrapper = mountCard()
    const regenerate = vi.spyOn(useAccessStore(), 'regenerateToken')
    vi.spyOn(useConfirmStore(), 'ask').mockResolvedValue(false)

    await wrapper.find('[data-testid="access-token-regenerate"]').trigger('click')
    await flushPromises()

    expect(regenerate).not.toHaveBeenCalled()
  })

  it('says so when regenerating fails', async () => {
    const wrapper = mountCard()
    vi.spyOn(useAccessStore(), 'regenerateToken').mockRejectedValue(new Error('nope'))
    vi.spyOn(useConfirmStore(), 'ask').mockResolvedValue(true)
    const show = vi.spyOn(useToastStore(), 'show')

    await wrapper.find('[data-testid="access-token-regenerate"]').trigger('click')
    await flushPromises()

    expect(show).toHaveBeenCalledWith('Could not regenerate the access token', true)
    expect(tokenField(wrapper).value).not.toContain('current-token')
  })
})
