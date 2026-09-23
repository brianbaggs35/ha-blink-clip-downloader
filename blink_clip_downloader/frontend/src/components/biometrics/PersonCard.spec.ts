import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import Avatar from 'primevue/avatar'
import ToggleSwitch from 'primevue/toggleswitch'
import PersonCard from './PersonCard.vue'
import { groupPeople } from './people'
import type { FaceEnrollment } from '../../api/types'
import { enrollment } from './testing'

function mountCard(photos: FaceEnrollment[]) {
  const [person] = groupPeople(photos, 640)
  return mount(PersonCard, { props: { person }, attachTo: document.body })
}

function photos(count: number, overrides: Partial<FaceEnrollment> = {}) {
  return Array.from({ length: count }, (_, i) => enrollment({ id: i + 1, ...overrides }))
}

describe('PersonCard', () => {
  it('shows up to four faces, then a count of the rest', () => {
    const wrapper = mountCard(photos(6))
    const avatars = wrapper.findAllComponents(Avatar)
    expect(avatars).toHaveLength(5)
    expect(avatars[0].props('image')).toBe('/api/ai/faces/thumbs/1')
    expect(avatars[4].props('label')).toBe('+2')
    expect(wrapper.text()).toContain('6 photos')
    expect(wrapper.text()).not.toContain('A few more photos')
  })

  it('falls back to initials, and tags an older enrollment', () => {
    const wrapper = mountCard(photos(1, { name: 'Mary Smith', has_thumbnail: false, frame_width: null, camera: null }))
    const avatars = wrapper.findAllComponents(Avatar)
    expect(avatars).toHaveLength(1)
    expect(avatars[0].props('label')).toBe('MS')
    expect(wrapper.text()).toContain('Earlier version')
    expect(wrapper.text()).toContain('1 photo')
    // Where it came from was never recorded, so nothing claims to know.
    expect(wrapper.find('.person-sources').exists()).toBe(false)
    // The page says what to do about it once, rather than every card.
    expect(wrapper.text()).not.toContain('A few more photos')
  })

  it('suggests more photos, from each camera, when there are only a couple', () => {
    const wrapper = mountCard(photos(2, { name: 'Amy' }))
    expect(wrapper.text()).toContain('A few more photos — from each camera Amy is seen on')
  })

  it('shows which cameras the photos came from, most first, uploads last', () => {
    const wrapper = mountCard([
      enrollment({ id: 1, camera: 'Front Door' }),
      enrollment({ id: 2, camera: 'Driveway' }),
      enrollment({ id: 3, camera: 'Driveway' }),
      enrollment({ id: 4, camera: null, frame_width: null }),
    ])
    const sources = wrapper.findAll('.person-sources li')
    expect(sources.map((li) => li.text())).toEqual(['Driveway 2', 'Front Door 1', 'Uploaded 1'])
    expect(sources.map((li) => li.find('i').classes())).toEqual([
      ['pi', 'pi-video'],
      ['pi', 'pi-video'],
      ['pi', 'pi-upload'],
    ])
  })

  it('shows approval, including a mix left by an older version', () => {
    expect(mountCard(photos(1)).text()).toContain('Approved')
    expect(mountCard(photos(1, { approved: false })).text()).toContain('Not approved')
    const mixed = mountCard([enrollment({ id: 1 }), enrollment({ id: 2, approved: false })])
    expect(mixed.text()).toContain('Partially approved')
    expect(mixed.findComponent(ToggleSwitch).props('modelValue')).toBe(false)
  })

  it('counts photos worth reviewing', () => {
    const wrapper = mountCard([
      enrollment({ id: 1, warning: { unlike_others: true, also_matches: '' } }),
      enrollment({ id: 2 }),
    ])
    expect(wrapper.text()).toContain('1 to review')
  })

  it('reports approval changes and the other actions', async () => {
    const wrapper = mountCard(photos(3))
    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    for (const label of ['Photos', 'Add photos', 'Remove']) {
      await wrapper
        .findAll('button')
        .find((b) => b.text() === label)!
        .trigger('click')
    }
    expect(wrapper.emitted('set-approved')).toEqual([[false]])
    expect(wrapper.emitted('manage')).toHaveLength(1)
    expect(wrapper.emitted('add-photos')).toHaveLength(1)
    expect(wrapper.emitted('remove')).toHaveLength(1)
  })

  it('renames in place, focused and pre-filled', async () => {
    const wrapper = mountCard(photos(1))
    await wrapper.find('button[aria-label="Rename"]').trigger('click')
    const input = wrapper.find('input.rename-input')
    expect((input.element as HTMLInputElement).value).toBe('Brian')
    expect(document.activeElement).toBe(input.element)

    await input.setValue('Bryan')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('rename')).toEqual([['Bryan']])
    expect(wrapper.find('form').exists()).toBe(false)
    wrapper.unmount()
  })

  it('can cancel a rename', async () => {
    const wrapper = mountCard(photos(1))
    await wrapper.find('button[aria-label="Rename"]').trigger('click')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Cancel')!
      .trigger('click')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.emitted('rename')).toBeUndefined()

    await wrapper.find('button[aria-label="Rename"]').trigger('click')
    await wrapper.find('form').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.emitted('rename')).toBeUndefined()
    wrapper.unmount()
  })

  it('labels its switch by an id a name with spaces cannot break', async () => {
    const wrapper = mountCard(photos(1, { name: 'Mary Ann Smith' }))
    const input = wrapper.find('input[role="switch"]')
    const id = input.attributes('id')!
    expect(id).not.toMatch(/\s/)
    expect(wrapper.find(`label[for="${id}"]`).text()).toBe('Approved for alert bypass')
    wrapper.unmount()
  })
})
