import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CodeBlock from './CodeBlock.vue'
import { useToastStore } from '../../stores/toast'

describe('CodeBlock', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the code text and copies it on click', async () => {
    const wrapper = mount(CodeBlock, { props: { code: 'alias: test' } })
    expect(wrapper.text()).toContain('alias: test')

    await wrapper.find('.copy-btn').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('alias: test')
    expect(useToastStore().message).toBe('Copied to clipboard')
  })

  it('shows an error toast if the clipboard write fails', async () => {
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    const wrapper = mount(CodeBlock, { props: { code: 'x' } })
    await wrapper.find('.copy-btn').trigger('click')
    await Promise.resolve()
    expect(useToastStore().isError).toBe(true)
  })
})
