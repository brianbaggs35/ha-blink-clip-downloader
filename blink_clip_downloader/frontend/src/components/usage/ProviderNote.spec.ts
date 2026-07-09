import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ProviderNote from './ProviderNote.vue'

describe('ProviderNote', () => {
  it.each([
    ['ollama', 'Ollama (Local/LAN)'],
    ['ollama_cloud', 'Ollama Cloud'],
    ['moondream_cloud', 'moondream.ai'],
    ['moondream_local', 'no cloud costs and no token tracking'],
    ['anthropic', 'Claude Haiku 4.5'],
    ['openai', 'OpenAI charges per token'],
  ])('renders the %s note', (provider, expected) => {
    const wrapper = mount(ProviderNote, { props: { provider, showEscalationNote: false } })
    expect(wrapper.text()).toContain(expected)
  })

  it('renders nothing for an unknown provider with no escalation note', () => {
    const wrapper = mount(ProviderNote, { props: { provider: 'mystery', showEscalationNote: false } })
    expect(wrapper.text().trim()).toBe('')
  })

  it('links to moondream.ai for moondream_cloud', () => {
    const wrapper = mount(ProviderNote, { props: { provider: 'moondream_cloud', showEscalationNote: false } })
    expect(wrapper.find('a').attributes('href')).toBe('https://moondream.ai')
  })

  it('appends the escalation note when showEscalationNote is true', () => {
    const wrapper = mount(ProviderNote, { props: { provider: 'anthropic', showEscalationNote: true } })
    expect(wrapper.text()).toContain('ai_escalation_provider')
  })

  it('shows only the escalation note when the provider is unknown', () => {
    const wrapper = mount(ProviderNote, { props: { showEscalationNote: true } })
    expect(wrapper.text()).toContain('ai_escalation_provider')
  })
})
