import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ToggleSwitch from 'primevue/toggleswitch'
import FoundFaces from './FoundFaces.vue'
import type { FoundFace } from './found'
import { candidate } from './testing'

function face(id: string, overrides: Partial<FoundFace> = {}): FoundFace {
  return {
    ...candidate(id),
    source: { kind: 'clip', clipId: 'c1', camera: 'Front Door', timestamp: '2026-01-01T10:00:00Z' },
    ...overrides,
  }
}

function mountFaces(faces: FoundFace[], groups: string[][], selected: string[] = []) {
  const wrapper = mount(FoundFaces, {
    props: {
      faces,
      groups,
      selected,
      'onUpdate:selected': (value: string[]) => wrapper.setProps({ selected: value }),
    },
  })
  return wrapper
}

const selectedIds = (wrapper: ReturnType<typeof mountFaces>) => wrapper.props('selected') as string[]

describe('FoundFaces', () => {
  it('shows the server’s groups, best face first, with where each face came from', () => {
    const wrapper = mountFaces(
      [
        face('a', { quality: 0.5 }),
        face('b', { quality: 0.9, time: 75 }),
        face('c', { source: { kind: 'photo', label: 'me.jpg' }, time: null }),
      ],
      [['a', 'b'], ['c']],
    )
    const groups = wrapper.findAll('.face-group')
    expect(groups).toHaveLength(2)
    expect(groups[0].text()).toContain('Person 1')
    expect(groups[0].text()).toContain('2 faces from 1 source')
    expect(groups[0].findAll('.face-tile')[0].text()).toContain('Front Door · 1:15')
    expect(groups[1].text()).toContain('me.jpg')
    expect(wrapper.text()).toContain('3 faces found')
  })

  it('still shows a face the server has not grouped', () => {
    const wrapper = mountFaces([face('a'), face('b', { time: null })], [['a', 'gone'], ['also-gone']])
    expect(wrapper.findAll('.face-group')).toHaveLength(2)
    expect(wrapper.findAll('.face-group')[1].text()).toContain('Front Door')
  })

  it('labels a group most of whose faces are already recognized, and offers to add to them', async () => {
    const recognized = { name: 'Brian', similarity: 0.9 }
    const wrapper = mountFaces(
      [
        face('a', { match: recognized }),
        face('b', { match: recognized }),
        face('c', { match: { name: 'Amy', similarity: 0.8 } }),
      ],
      [['a', 'b', 'c']],
    )
    expect(wrapper.text()).toContain('Already recognized as Brian')
    expect(wrapper.text()).toContain('Recognized as Amy')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Add to Brian')!
      .trigger('click')
    expect(wrapper.emitted('add-to')).toEqual([['Brian', ['a', 'b', 'c']]])
  })

  it('picks and unpicks faces one at a time', async () => {
    const wrapper = mountFaces([face('a'), face('b')], [['a', 'b']])
    const [first, second] = wrapper.findAll('.face-tile')
    await first.trigger('click')
    await second.trigger('click')
    expect(selectedIds(wrapper)).toEqual(['a', 'b'])
    expect(wrapper.findAll('.face-tile')[0].attributes('aria-pressed')).toBe('true')
    await wrapper.findAll('.face-tile')[0].trigger('click')
    expect(selectedIds(wrapper)).toEqual(['b'])
  })

  it('selects and deselects a whole group', async () => {
    const wrapper = mountFaces([face('a'), face('b'), face('c')], [['a', 'b'], ['c']], ['c'])
    const groupButton = () =>
      wrapper
        .findAll('.face-group')[0]
        .findAll('button')
        .find((b) => /Select all|Deselect all/.test(b.text()))!
    await groupButton().trigger('click')
    expect(selectedIds(wrapper).sort()).toEqual(['a', 'b', 'c'])
    expect(groupButton().text()).toBe('Deselect all')
    await groupButton().trigger('click')
    expect(selectedIds(wrapper)).toEqual(['c'])
  })

  it('hides low-quality faces until asked, and unpicks them when hidden again', async () => {
    const wrapper = mountFaces([face('good'), face('poor', { quality: 0.1 })], [['good', 'poor']])
    expect(wrapper.findAll('.face-tile')).toHaveLength(1)
    expect(wrapper.text()).toContain('Show 1 low-quality face')

    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', true)
    expect(wrapper.findAll('.face-tile')).toHaveLength(2)
    await wrapper.findAll('.face-tile')[1].trigger('click')
    await wrapper.findAll('.face-tile')[0].trigger('click')
    expect(selectedIds(wrapper)).toEqual(['poor', 'good'])

    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    expect(selectedIds(wrapper)).toEqual(['good'])
  })

  it('keeps a picked id it knows nothing about when hiding low-quality faces', async () => {
    const wrapper = mountFaces([face('poor', { quality: 0.1 })], [['poor']], ['elsewhere'])
    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', true)
    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    expect(selectedIds(wrapper)).toEqual(['elsewhere'])
  })

  it('explains when every face found is too poor to offer', () => {
    const wrapper = mountFaces(
      [face('poor', { quality: 0.1 }), face('worse', { quality: 0.05 })],
      [['poor'], ['worse']],
    )
    expect(wrapper.findAll('.face-group')).toHaveLength(0)
    expect(wrapper.text()).toContain('too small or blurred')
    expect(wrapper.text()).toContain('Show 2 low-quality faces')
  })

  it('reads well for a single face from a single source', () => {
    const wrapper = mountFaces([face('a')], [['a']])
    expect(wrapper.text()).toContain('1 face found')
    expect(wrapper.text()).toContain('1 face from 1 source')
    expect(wrapper.find('.low-quality-toggle').exists()).toBe(false)
  })
})
