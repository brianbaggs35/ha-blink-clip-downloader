import { afterEach, describe, expect, it } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import PersonPhotosDialog from './PersonPhotosDialog.vue'
import { groupPeople, type Person } from './people'
import { enrollment } from './testing'

const PERSON = groupPeople(
  [
    enrollment({ id: 1 }),
    enrollment({ id: 2, has_thumbnail: false, frame_width: null }),
    enrollment({ id: 3, frame_width: 1280, warning: { unlike_others: true, also_matches: 'Amy' } }),
  ],
  640,
)[0]

async function mountDialog(person: Person | null = PERSON, busy = false) {
  const wrapper = mount(PersonPhotosDialog, { props: { person, frameWidth: 640, busy, visible: true } })
  await flushPromises()
  return wrapper
}

// Dialog teleports its content to document.body.
const body = () => new DOMWrapper(document.body)
const button = (label: string) =>
  body()
    .findAll('button')
    .find((b) => b.text().includes(label))!

describe('PersonPhotosDialog', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('shows every photo of the person, with anything wrong with it', async () => {
    await mountDialog()
    expect(body().text()).toContain("Brian's photos")
    const items = body().findAll('.photo-item')
    expect(items).toHaveLength(3)
    expect(items[0].find('img').attributes('src')).toBe('/api/ai/faces/thumbs/1')
    expect(items[1].find('img').exists()).toBe(false)
    expect(items[1].text()).toContain('no image kept')
    expect(items[1].classes()).not.toContain('flagged')
    expect(items[2].classes()).toContain('flagged')
    expect(items[2].text()).toContain("Doesn't look like")
    expect(items[2].text()).toContain('Also looks like Amy')
    expect(items[2].text()).toContain('Captured at 1280px')
  })

  it('asks to remove a photo, or to add more', async () => {
    const wrapper = await mountDialog()
    await button('Remove photo').trigger('click')
    await button('Add photos').trigger('click')
    expect(wrapper.emitted('remove-photo')).toEqual([[1]])
    expect(wrapper.emitted('add-photos')).toHaveLength(1)
  })

  it('closes', async () => {
    const wrapper = await mountDialog()
    await button('Close').trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('closes from its own close button', async () => {
    const wrapper = await mountDialog()
    await body().find('.p-dialog-close-button').trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('holds removal while a change is in flight', async () => {
    await mountDialog(PERSON, true)
    expect(button('Remove photo').attributes('disabled')).toBeDefined()
  })

  it('renders nothing to act on without a person', async () => {
    await mountDialog(null)
    expect(body().find('.p-dialog-title').text()).toBe('Photos')
    expect(body().findAll('.photo-item')).toHaveLength(0)
    expect(body().text()).toContain("aren't them")
  })
})
