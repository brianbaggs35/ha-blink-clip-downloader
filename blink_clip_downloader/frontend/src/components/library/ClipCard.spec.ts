import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ClipCard from './ClipCard.vue'
import type { ClipListItem } from '../../api/types'

const CLIP: ClipListItem = {
  id: 'c1',
  camera: 'front',
  file_path: '/data/clips/front.mp4',
  timestamp: '2026-01-05T10:00:00Z',
  size_bytes: 5_000_000,
  duration: 65,
  source: 'pir',
  network_id: 1,
  starred: true,
  tags: ['delivery'],
  downloaded_at: '2026-01-05T10:01:00Z',
  archived: false,
  archive_path: '',
  notified: true,
}

describe('ClipCard', () => {
  it('renders camera, duration, size, tags, and star/notified badges', () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: false } })
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('1m 5s')
    expect(wrapper.text()).toContain('pir')
    expect(wrapper.text()).toContain('delivery')
    expect(wrapper.find('.star-badge').exists()).toBe(true)
    expect(wrapper.find('.notified-badge').exists()).toBe(true)
  })

  it('shows a checkmark and the selected class when selected', () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: true } })
    expect(wrapper.find('.clip-card').classes()).toContain('selected')
    expect(wrapper.find('.sel-check').text()).toBe('✓')
  })

  it('falls back to a placeholder icon when the thumbnail fails to load', async () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: false } })
    expect(wrapper.find('img').exists()).toBe(true)
    await wrapper.find('img').trigger('error')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('.no-thumb').exists()).toBe(true)
  })

  it('omits the duration badge, source pill, and star/notified badges when absent', () => {
    const minimal: ClipListItem = { ...CLIP, duration: 0, source: '', starred: false, notified: false, tags: [] }
    const wrapper = mount(ClipCard, { props: { clip: minimal, selected: false } })
    expect(wrapper.find('.dur-badge').exists()).toBe(false)
    expect(wrapper.find('.src-pill').exists()).toBe(false)
    expect(wrapper.find('.star-badge').exists()).toBe(false)
    expect(wrapper.find('.notified-badge').exists()).toBe(false)
    expect(wrapper.find('.clip-meta').text()).not.toContain('⏱')
  })

  it('shows duration as text in the meta row alongside camera/date/size, not just as a thumbnail badge', () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: false } })
    const meta = wrapper.find('.clip-meta').text()
    expect(meta).toContain('⏱')
    expect(meta).toContain('1m 5s')
  })

  it('emits click', async () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: false } })
    await wrapper.find('.clip-card').trigger('click')
    expect(wrapper.emitted('click')).toHaveLength(1)
  })

  it('clicking the checkbox emits check but not click', async () => {
    const wrapper = mount(ClipCard, { props: { clip: CLIP, selected: false } })
    await wrapper.find('.sel-check').trigger('click')
    expect(wrapper.emitted('check')).toHaveLength(1)
    expect(wrapper.emitted('click')).toBeUndefined()
  })
})
