import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import RecipeBuilder from './RecipeBuilder.vue'
import type { Recipe } from './recipes/types'

const RECIPES: Recipe[] = [
  {
    id: 'first',
    name: 'First recipe',
    group: 'Storage',
    icon: '💾',
    description: 'The first one.',
    target: 'automations.yaml',
    filename: 'first.yaml',
    fields: [
      { key: 'threshold', label: 'Notify above', type: 'number', default: 80, suffix: '%' },
      { key: 'cameras', label: 'Cameras', type: 'multiselect', default: [], source: 'cameras' },
    ],
    build: (v) => `alias: first\nthreshold: ${v.threshold}`,
  },
  {
    id: 'second',
    name: 'Second recipe',
    group: 'Security',
    icon: '🚨',
    description: 'The second one.',
    target: 'scripts.yaml',
    filename: 'second.yaml',
    fields: [{ key: 'name', label: 'Name', type: 'text', default: 'alpha' }],
    build: (v) => `alias: ${v.name}`,
  },
]

function mountBuilder(recipes: Recipe[] = RECIPES, cameras: string[] = ['Front Door']) {
  return mount(RecipeBuilder, {
    props: { recipes, cameras, storageKey: 'test.recipe' },
  })
}

describe('RecipeBuilder', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('opens on the first recipe and previews its YAML', () => {
    const wrapper = mountBuilder()
    expect(wrapper.text()).toContain('First recipe')
    expect(wrapper.text()).toContain('The first one.')
    expect(wrapper.find('.code-block').text()).toContain('threshold: 80')
  })

  it('groups the picker by the recipes own groups', () => {
    const groups = mountBuilder().findComponent({ name: 'Listbox' }).props('options') as {
      label: string
      items: Recipe[]
    }[]
    expect(groups.map((g) => g.label)).toEqual(['Storage', 'Security'])
  })

  it('regenerates the preview as a field changes', async () => {
    const wrapper = mountBuilder()
    await wrapper.findComponent({ name: 'InputNumber' }).vm.$emit('update:modelValue', 95)
    expect(wrapper.find('.code-block').text()).toContain('threshold: 95')
  })

  it('switches recipe, resets the form, and remembers the choice', async () => {
    const wrapper = mountBuilder()
    await wrapper.findComponent({ name: 'Listbox' }).vm.$emit('update:modelValue', 'second')
    expect(wrapper.text()).toContain('Second recipe')
    expect(wrapper.find('.code-block').text()).toContain('alias: alpha')
    expect(localStorage.getItem('test.recipe')).toBe('second')

    const reopened = mountBuilder()
    expect(reopened.text()).toContain('Second recipe')
  })

  it('ignores an empty selection rather than blanking the form', async () => {
    const wrapper = mountBuilder()
    await wrapper.findComponent({ name: 'Listbox' }).vm.$emit('update:modelValue', '')
    expect(wrapper.text()).toContain('First recipe')
  })

  it('falls back to the first recipe if the remembered one is gone', () => {
    localStorage.setItem('test.recipe', 'removed-in-an-update')
    expect(mountBuilder().text()).toContain('First recipe')
  })

  it('resets edited values back to the recipe defaults', async () => {
    const wrapper = mountBuilder()
    await wrapper.findComponent({ name: 'InputNumber' }).vm.$emit('update:modelValue', 95)
    await wrapper.findComponent({ name: 'Button' }).trigger('click')
    expect(wrapper.find('.code-block').text()).toContain('threshold: 80')
  })

  it('names the file the download button will use', () => {
    expect(mountBuilder().findComponent({ name: 'CodeBlock' }).props('filename')).toBe('first.yaml')
  })

  it('says so when there are no cameras to filter on', () => {
    expect(mountBuilder(RECIPES, []).text()).toContain('No cameras to choose from yet')
    expect(mountBuilder(RECIPES, ['Front Door']).text()).not.toContain('No cameras to choose from yet')
  })

  it('tells the user where the generated YAML goes', () => {
    expect(mountBuilder().text()).toContain('Settings → Automations & scenes')
  })

  it('keeps the page alive when a recipe throws mid-edit', () => {
    const throwing: Recipe[] = [
      {
        ...RECIPES[0],
        build: () => {
          throw new Error('boom')
        },
      },
    ]
    expect(mountBuilder(throwing).find('.code-block').text()).toContain('Could not generate')
  })

  it('says nothing about where the YAML goes for an output with no click path', () => {
    const odd: Recipe[] = [{ ...RECIPES[0], target: 'somewhere-else.yaml' }]
    const wrapper = mountBuilder(odd)
    expect(wrapper.find('.recipe-hint').exists()).toBe(false)
  })

  it('does not fall over when localStorage is unavailable', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const wrapper = mountBuilder()
    expect(() => wrapper.findComponent({ name: 'Listbox' }).vm.$emit('update:modelValue', 'second')).not.toThrow()
    spy.mockRestore()
  })
})
