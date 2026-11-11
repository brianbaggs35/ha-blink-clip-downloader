import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AssistantMessageBubble from './AssistantMessageBubble.vue'
import type { AssistantMessage } from '../../api/types'

const ANSWER: AssistantMessage = {
  role: 'assistant',
  content: 'One clip was flagged.',
  at: '2026-11-10T22:00:00Z',
  model: 'claude-x',
  days: 1,
  prompt_tokens: 1000,
  completion_tokens: 234,
  clips: [
    { ref: 'C1', id: 'clip-1', camera: 'Driveway', timestamp: '2026-11-10T21:00:00Z', is_suspicious: true },
    { ref: 'C2', id: 'clip-2', camera: 'Porch', timestamp: '2026-11-10T20:00:00Z', is_suspicious: false },
  ],
}

describe('AssistantMessageBubble', () => {
  it('shows a question as plain text with no footer', () => {
    const w = mount(AssistantMessageBubble, {
      props: { message: { role: 'user', content: 'Anything today?', at: '2026-11-10T22:00:00Z' } },
    })
    expect(w.attributes('data-role')).toBe('user')
    expect(w.text()).toBe('Anything today?')
    expect(w.find('[data-testid="assistant-clips"]').exists()).toBe(false)
  })

  it('shows an answer with its model, tokens and look-back window', () => {
    const w = mount(AssistantMessageBubble, { props: { message: ANSWER } })
    expect(w.text()).toContain('One clip was flagged.')
    expect(w.text()).toContain('claude-x')
    expect(w.text()).toContain('1.2K tokens')
    expect(w.text()).toContain('last 1 day of activity')
  })

  it('pluralizes the window and leaves out figures that are missing', () => {
    const w = mount(AssistantMessageBubble, {
      props: { message: { role: 'assistant', content: 'Nothing.', at: 'x', model: 'm', days: 7 } },
    })
    expect(w.text()).toContain('last 7 days of activity')
    expect(w.text()).not.toContain('tokens')
  })

  it('renders no footer for an answer carrying no details', () => {
    const w = mount(AssistantMessageBubble, { props: { message: { role: 'assistant', content: 'Hi', at: 'x' } } })
    expect(w.find('.bubble-foot').exists()).toBe(false)
  })

  it('lets a mentioned clip be opened, flagging the suspicious one', async () => {
    const w = mount(AssistantMessageBubble, { props: { message: ANSWER } })
    const chips = w.findAll('[data-testid="assistant-clip-chip"]')
    expect(chips).toHaveLength(2)
    expect(chips[0].classes().join(' ')).toContain('danger')
    expect(chips[1].classes().join(' ')).not.toContain('danger')
    await chips[1].trigger('click')
    expect(w.emitted('open-clip')).toEqual([['clip-2']])
  })
})
