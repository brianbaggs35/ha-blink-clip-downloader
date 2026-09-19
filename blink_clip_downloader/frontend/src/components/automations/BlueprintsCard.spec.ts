import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import BlueprintsCard from './BlueprintsCard.vue'
import { BLUEPRINTS } from './recipes/blueprints'

describe('BlueprintsCard', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists every blueprint with a downloadable file', () => {
    const wrapper = mount(BlueprintsCard)
    for (const bp of BLUEPRINTS) expect(wrapper.text()).toContain(bp.name)
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    expect(blocks.map((b) => b.props('filename'))).toEqual(BLUEPRINTS.map((bp) => bp.filename))
  })

  it('explains where the file goes and how to reload it', () => {
    const text = mount(BlueprintsCard).text()
    expect(text).toContain('config/blueprints/automation')
    expect(text).toContain('Blueprints')
  })
})
