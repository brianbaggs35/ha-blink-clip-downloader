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

  it('has no download button until a filename is offered', () => {
    const wrapper = mount(CodeBlock, { props: { code: 'x' } })
    expect(wrapper.findAll('.copy-btn').map((b) => b.text())).toEqual(['Copy'])
  })

  it('downloads the code as the named file', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:fake')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const wrapper = mount(CodeBlock, { props: { code: 'alias: x', filename: 'blink.yaml' } })
    await wrapper.findAll('.copy-btn')[1].trigger('click')

    expect(createObjectURL).toHaveBeenCalled()
    expect(click).toHaveBeenCalled()
    // Revoked straight away: the anchor is never in the document, so nothing
    // else is holding the blob.
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake')
    expect(useToastStore().message).toBe('Downloaded blink.yaml')
    click.mockRestore()
  })

  it('reports a download that the browser refuses', async () => {
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: () => {
        throw new Error('blocked')
      },
    })
    const wrapper = mount(CodeBlock, { props: { code: 'x', filename: 'blink.yaml' } })
    await wrapper.findAll('.copy-btn')[1].trigger('click')
    expect(useToastStore().isError).toBe(true)
  })

  it('renders the code with no whitespace of its own around it', () => {
    // The block renders with `white-space: pre`, so template indentation
    // next to the interpolation would show as a leading space on the first
    // line — and YAML copied by selection rather than by the Copy button
    // would then be indented, which changes what it means.
    const wrapper = mount(CodeBlock, { props: { code: 'alias: x', filename: 'a.yaml' } })
    expect(wrapper.find('.code-block-text').element.textContent).toBe('alias: x')
  })
})
