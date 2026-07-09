import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import CameraNav from './CameraNav.vue'

const CAMERAS = [
  { camera: 'front', total: 10, size_bytes: 0, today: 1, this_week: 2, last_seen: '' },
  { camera: 'back', total: 5, size_bytes: 0, today: 0, this_week: 1, last_seen: '' },
]

describe('CameraNav', () => {
  it('shows "All Cameras" with the sum of every camera total', () => {
    const wrapper = mount(CameraNav, { props: { cameras: CAMERAS, modelValue: 'all' } })
    const all = wrapper.find('[data-camera="all"]')
    expect(all.classes()).toContain('active')
    expect(all.text()).toContain('15')
  })

  it('renders one entry per camera with its own total', () => {
    const wrapper = mount(CameraNav, { props: { cameras: CAMERAS, modelValue: 'all' } })
    expect(wrapper.find('[data-camera="front"]').text()).toContain('10')
    expect(wrapper.find('[data-camera="back"]').text()).toContain('5')
  })

  it('marks the currently-selected camera active', () => {
    const wrapper = mount(CameraNav, { props: { cameras: CAMERAS, modelValue: 'front' } })
    expect(wrapper.find('[data-camera="front"]').classes()).toContain('active')
    expect(wrapper.find('[data-camera="all"]').classes()).not.toContain('active')
  })

  it('emits update:modelValue when a camera is clicked', async () => {
    const wrapper = mount(CameraNav, { props: { cameras: CAMERAS, modelValue: 'all' } })
    await wrapper.find('[data-camera="back"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['back']])
  })

  it('handles a camera with a falsy total', () => {
    const wrapper = mount(CameraNav, {
      props: { cameras: [{ camera: 'empty', total: 0, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }], modelValue: 'all' },
    })
    expect(wrapper.find('[data-camera="empty"]').text()).toContain('0')
    expect(wrapper.find('[data-camera="all"]').text()).toContain('0')
  })

  it('renders with no cameras at all', () => {
    const wrapper = mount(CameraNav, { props: { cameras: [], modelValue: 'all' } })
    expect(wrapper.find('[data-camera="all"]').text()).toContain('0')
  })
})
