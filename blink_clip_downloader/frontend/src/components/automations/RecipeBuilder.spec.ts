import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useToastStore } from '../../stores/toast'
import { useAccessStore } from '../../stores/access'
import RecipeBuilder from './RecipeBuilder.vue'
import type { Recipe } from './recipes/types'
import * as haConfig from '../../api/haConfig'

/** The name Home Assistant would list the first recipe under — what the
 * backend reports on a successful create, not an entity id. */
const CREATED_NAME = 'Blink – first'

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
    create: { kind: 'automation', objectId: 'blink_first' },
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

  it('says so when nothing is marked for an asset-sourced field to offer', () => {
    const withAssetField: Recipe[] = [
      {
        ...RECIPES[1],
        fields: [{ key: 'assets', label: 'Assets', type: 'multiselect', default: [], source: 'assets' }],
      },
    ]
    const empty = mount(RecipeBuilder, {
      props: { recipes: withAssetField, cameras: [], storageKey: 'test.assets' },
    })
    expect(empty.text()).toContain('Nothing is marked on the Assets tab yet')
    const marked = mount(RecipeBuilder, {
      props: { recipes: withAssetField, cameras: [], assets: ['Mailbox'], storageKey: 'test.assets' },
    })
    expect(marked.text()).not.toContain('Nothing is marked on the Assets tab yet')
    expect(marked.findComponent({ name: 'RecipeFieldInput' }).props('assets')).toEqual(['Mailbox'])
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

  describe('creating it in Home Assistant', () => {
    it('sends the recipe kind, its stable id and the YAML on screen', async () => {
      const spy = vi.spyOn(haConfig, 'createInHomeAssistant').mockResolvedValue({ created: true, name: CREATED_NAME })
      const wrapper = mountBuilder()
      await wrapper.findComponent({ name: 'InputNumber' }).vm.$emit('update:modelValue', 95)

      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')
      await flushPromises()

      expect(spy).toHaveBeenCalledWith('automation', 'blink_first', 'alias: first\nthreshold: 95')
      expect(useToastStore().message).toContain(CREATED_NAME)
      expect(wrapper.text()).toContain('updates that same one rather than adding another')
    })

    it('shows the refusal verbatim when Home Assistant will not take it', async () => {
      vi.spyOn(haConfig, 'createInHomeAssistant').mockResolvedValue({
        created: false,
        message: 'Home Assistant refused the request.',
      })
      const wrapper = mountBuilder()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')
      await flushPromises()

      expect(useToastStore().message).toBe('Home Assistant refused the request.')
      expect(useToastStore().isError).toBe(true)
    })

    it('reports a refusal with no message of its own', async () => {
      vi.spyOn(haConfig, 'createInHomeAssistant').mockResolvedValue({ created: false })
      const wrapper = mountBuilder()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')
      await flushPromises()
      expect(useToastStore().message).toBe('Home Assistant would not create it')
    })

    it('says so when the add-on cannot reach Home Assistant at all', async () => {
      vi.spyOn(haConfig, 'createInHomeAssistant').mockRejectedValue(new Error('503'))
      const wrapper = mountBuilder()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')
      await flushPromises()
      expect(useToastStore().isError).toBe(true)
      expect(wrapper.text()).not.toContain('now exists in Home Assistant')
    })

    it('does not credit a different recipe with what the last one created', async () => {
      // The create is still in flight when the user picks another recipe.
      // The toast still reports the truth; the banner under the new recipe
      // must not claim that recipe now exists.
      let settle: (v: { created: boolean; name: string }) => void = () => {}
      vi.spyOn(haConfig, 'createInHomeAssistant').mockReturnValue(
        new Promise((resolve) => {
          settle = resolve
        }),
      )
      const wrapper = mountBuilder()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')

      wrapper.findComponent({ name: 'Listbox' }).vm.$emit('update:modelValue', 'second')
      await wrapper.vm.$nextTick()
      settle({ created: true, name: CREATED_NAME })
      await flushPromises()

      expect(useToastStore().message).toContain(CREATED_NAME)
      expect(wrapper.text()).not.toContain('now exists in Home Assistant')
    })

    it('offers no Create button for a recipe with no API behind it', () => {
      const copyOnly: Recipe[] = [{ ...RECIPES[0], create: undefined }]
      const wrapper = mountBuilder(copyOnly)
      expect(wrapper.findAll('button').some((b) => b.text().includes('Create'))).toBe(false)
      expect(wrapper.text()).toContain('no API to create it from here')
    })

    it('clears the created banner when a different recipe is picked', async () => {
      vi.spyOn(haConfig, 'createInHomeAssistant').mockResolvedValue({
        created: true,
        name: CREATED_NAME,
      })
      const wrapper = mountBuilder()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Create'))!
        .trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('now exists in Home Assistant')

      await wrapper.findComponent({ name: 'Listbox' }).vm.$emit('update:modelValue', 'second')
      await flushPromises()
      expect(wrapper.text()).not.toContain('now exists in Home Assistant')
    })
  })

  it('hands the access token to the recipe alongside its own fields', () => {
    useAccessStore().accessToken = 'tok'
    const echo: Recipe = { ...RECIPES[1], build: (v) => `token: ${v.access_token}\nname: ${v.name}` }
    const wrapper = mountBuilder([echo])
    const code = wrapper.findComponent({ name: 'CodeBlock' }).props('code') as string
    expect(code).toContain('token: tok')
    expect(code).toContain('name: alpha')
  })
})
