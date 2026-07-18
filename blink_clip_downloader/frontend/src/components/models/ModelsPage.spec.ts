import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ModelsPage from './ModelsPage.vue'

describe('ModelsPage', () => {
  it('lists all six AI providers with their config keys', () => {
    const wrapper = mount(ModelsPage)
    for (const key of ['ollama', 'ollama_cloud', 'moondream_cloud', 'moondream_local', 'anthropic', 'openai']) {
      expect(wrapper.text()).toContain(key)
    }
    expect(wrapper.text()).toContain('Ollama (Local/LAN)')
    expect(wrapper.text()).toContain('Anthropic (Claude)')
    expect(wrapper.text()).toContain('OpenAI (GPT)')
  })

  it("links out to each provider's docs/console instead of hardcoding pricing", () => {
    const wrapper = mount(ModelsPage)
    const hrefs = wrapper.findAll('a').map((a) => a.attributes('href'))
    expect(hrefs).toContain('https://console.anthropic.com')
    expect(hrefs).toContain('https://platform.openai.com')
    expect(hrefs).toContain('https://docs.moondream.ai/api')
    expect(wrapper.text()).not.toMatch(/\$\d/)
  })

  it('links to a pricing page for each paid/cloud provider', () => {
    const wrapper = mount(ModelsPage)
    const hrefs = wrapper.findAll('a').map((a) => a.attributes('href'))
    expect(hrefs).toContain('https://developers.openai.com/api/docs/pricing')
    expect(hrefs).toContain('https://platform.claude.com/docs/en/about-claude/pricing')
    expect(hrefs).toContain('https://docs.moondream.ai/pricing/')
    expect(hrefs).toContain('https://ollama.com/pricing')
    expect(wrapper.text()).toContain('💲 Pricing')
  })

  it('links to documentation instead of pricing for the local (free) providers', () => {
    const wrapper = mount(ModelsPage)
    const hrefs = wrapper.findAll('a').map((a) => a.attributes('href'))
    expect(hrefs).toContain('https://docs.ollama.com/')
    expect(hrefs).toContain('https://docs.moondream.ai/running-locally/')
    expect(wrapper.text()).toContain('📖 Documentation')
  })

  it('every external link opens in a new tab safely', () => {
    const wrapper = mount(ModelsPage)
    for (const a of wrapper.findAll('a')) {
      expect(a.attributes('target')).toBe('_blank')
      expect(a.attributes('rel')).toBe('noopener')
    }
  })

  it('mentions the moondream_local GPU requirement', () => {
    const wrapper = mount(ModelsPage)
    expect(wrapper.text()).toContain('NVIDIA CUDA or Apple Silicon GPU')
  })

  it('explains two-tier escalation', () => {
    const wrapper = mount(ModelsPage)
    expect(wrapper.text()).toContain('ai_escalation_provider')
    expect(wrapper.text()).toContain('AI Usage')
  })

  it('documents the CV pipeline/face recognition CPU requirement', () => {
    const wrapper = mount(ModelsPage)
    expect(wrapper.text()).toContain('ARMv8.1+ LSE atomic instructions')
    expect(wrapper.text()).toContain('Raspberry Pi 5')
    expect(wrapper.text()).toContain('Raspberry Pi 4')
    expect(wrapper.text()).toContain('ai_enhanced_detection_enabled')
    expect(wrapper.text()).toContain('ai_face_recognition_enabled')
  })
})
