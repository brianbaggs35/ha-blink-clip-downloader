import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import BiometricsPage from './BiometricsPage.vue'
import PersonCard from './PersonCard.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import type { FaceEnrollment, FacesResponse } from '../../api/types'
import { deferred, enrollment, errorResponse, jsonResponse } from './testing'

const addPhotosFor = vi.fn()
const scanClip = vi.fn()
const reveal = vi.fn()
const FinderStub = defineComponent({
  name: 'FaceFinder',
  props: { available: Boolean, people: { type: Array, default: () => [] } },
  emits: ['enrolled'],
  setup(_, { expose }) {
    expose({ addPhotosFor, scanClip, reveal })
    return {}
  },
  template: '<div class="finder-stub" />',
})
const ActivityStub = defineComponent({
  name: 'FaceBypassActivityCard',
  emits: ['scan-clip'],
  template: '<div class="activity-stub" />',
})

function facesResponse(faces: FaceEnrollment[], overrides: Partial<FacesResponse> = {}): FacesResponse {
  return {
    available: true,
    recognition_enabled: true,
    analysis_enabled: true,
    frame_width: 640,
    faces,
    ...overrides,
  }
}

type Handler = (url: string, init?: RequestInit) => Promise<Response> | undefined

function stubFetch(list: () => FacesResponse | Promise<Response>, handler: Handler = () => undefined) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url === '/api/ai/faces' && (!init?.method || init.method === 'GET')) {
      const result = list()
      return result instanceof Promise ? result : Promise.resolve(jsonResponse(result))
    }
    return handler(url, init) ?? Promise.resolve(jsonResponse({ updated: true, deleted: true }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function mountPage() {
  const wrapper = mount(BiometricsPage, {
    global: { stubs: { FaceFinder: FinderStub, FaceBypassActivityCard: ActivityStub } },
  })
  await flushPromises()
  return wrapper
}

function card(wrapper: Awaited<ReturnType<typeof mountPage>>, name: string) {
  return wrapper.findAllComponents(PersonCard).find((c) => c.props('person').name === name)!
}

function writes(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method && init.method !== 'GET')
    .map(([url, init]) => [init!.method, url, init!.body ? JSON.parse(init!.body as string) : undefined])
}

async function answer(result: boolean) {
  await flushPromises()
  useConfirmStore().settle(result)
  await flushPromises()
}

const BRIAN = [enrollment({ id: 1 }), enrollment({ id: 2 })]
const PEOPLE = [...BRIAN, enrollment({ id: 3, name: 'Amy', approved: false })]

describe('BiometricsPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    addPhotosFor.mockReset()
    scanClip.mockReset()
    reveal.mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('shows placeholders while loading, then everyone enrolled', async () => {
    const pending = deferred<Response>()
    stubFetch(() => pending.promise)
    const wrapper = mount(BiometricsPage, {
      global: { stubs: { FaceFinder: FinderStub, FaceBypassActivityCard: ActivityStub } },
    })
    await flushPromises()
    expect(wrapper.findAll('.p-skeleton').length).toBeGreaterThan(0)

    pending.resolve(jsonResponse(facesResponse(PEOPLE)))
    await flushPromises()
    expect(wrapper.findAllComponents(PersonCard).map((c) => c.props('person').name)).toEqual(['Amy', 'Brian'])
    expect(wrapper.text()).toContain('1 of 2 enrolled people are approved')
    expect(wrapper.findComponent(FinderStub).props('people')).toHaveLength(2)
  })

  it('invites a first enrollment when nobody is enrolled', async () => {
    stubFetch(() => facesResponse([]))
    const wrapper = await mountPage()
    // e2e/smoke.mjs waits for exactly this text on an empty install.
    expect(wrapper.text()).toContain('Nobody enrolled yet')
    expect(wrapper.text()).toContain('stays local')
    expect(wrapper.text()).toContain('all-or-nothing')
    await wrapper.find('.empty-state button').trigger('click')
    expect(reveal).toHaveBeenCalled()
  })

  it('cannot start finding faces where recognition cannot run', async () => {
    stubFetch(() => facesResponse([], { available: false }))
    const wrapper = await mountPage()
    expect(wrapper.find('.empty-state button').attributes('disabled')).toBeDefined()
  })

  it('says once, not on every card, who was enrolled by an earlier version', async () => {
    const legacy = { has_thumbnail: false, frame_width: null, camera: null }
    stubFetch(() =>
      facesResponse([
        enrollment({ id: 1, name: 'Amy', ...legacy }),
        enrollment({ id: 2, name: 'Ben', ...legacy }),
        enrollment({ id: 3, name: 'Cal' }),
      ]),
    )
    let wrapper = await mountPage()
    expect(wrapper.text()).toContain('2 people were enrolled with an earlier version')

    stubFetch(() => facesResponse([enrollment({ id: 1, name: 'Amy', ...legacy })]))
    wrapper = await mountPage()
    expect(wrapper.text()).toContain('1 person was enrolled with an earlier version')

    stubFetch(() => facesResponse(BRIAN))
    wrapper = await mountPage()
    expect(wrapper.text()).not.toContain('earlier version')
  })

  it('reads naturally for a single person', async () => {
    stubFetch(() => facesResponse(BRIAN))
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('1 of 1 enrolled person is approved')
  })

  it('says when the list could not load', async () => {
    stubFetch(() => Promise.resolve(errorResponse(500)))
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain("Couldn't load enrolled people")
    expect(wrapper.findComponent(FinderStub).props('available')).toBe(true)
  })

  it.each([
    [{ available: false }, 'dependencies are not available'],
    [{ recognition_enabled: false }, 'Enable Local Face Recognition'],
    [{ analysis_enabled: false }, 'an AI provider is set up'],
  ])('explains why nobody would be recognized: %o', async (overrides, text) => {
    stubFetch(() => facesResponse(BRIAN, overrides))
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain(text)
  })

  it('points out photos captured at another resolution', async () => {
    stubFetch(() => facesResponse([enrollment({ frame_width: 960 })], { frame_width: 1280 }))
    let wrapper = await mountPage()
    expect(wrapper.text()).toContain('1 photo was captured at a different Face Recognition Resolution')
    expect(wrapper.text()).toContain('1280px')

    stubFetch(() =>
      facesResponse([enrollment({ frame_width: 960 }), enrollment({ id: 2, frame_width: 960 })], { frame_width: 1280 }),
    )
    wrapper = await mountPage()
    expect(wrapper.text()).toContain('2 photos were captured')
    expect(wrapper.text()).toContain("each person's Photos list shows which")
  })

  it('approves and unapproves a person', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Amy').vm.$emit('set-approved', true)
    await flushPromises()
    expect(writes(fetchMock)).toEqual([['PATCH', '/api/ai/faces/people', { name: 'Amy', approved: true }]])
    expect(useToastStore().message).toBe('Amy can now clear alerts')

    card(wrapper, 'Brian').vm.$emit('set-approved', false)
    await flushPromises()
    expect(useToastStore().message).toBe('Brian no longer clears alerts')
  })

  it('says so when a change fails, with the reason, and reloads either way', async () => {
    const fetchMock = stubFetch(
      () => facesResponse(PEOPLE),
      (_url, init) =>
        init?.method === 'PATCH' && JSON.parse(init.body as string).new_name
          ? Promise.resolve(errorResponse(400, JSON.stringify({ error: 'Enter a name of up to 60 characters' })))
          : Promise.reject(new TypeError('Failed to fetch')),
    )
    const wrapper = await mountPage()
    const loads = () => fetchMock.mock.calls.filter(([u, i]) => u === '/api/ai/faces' && !i?.method).length
    const loadsBefore = loads()
    card(wrapper, 'Amy').vm.$emit('set-approved', true)
    await flushPromises()
    expect(useToastStore().message).toBe('Failed to update approval: check your connection and try again')
    expect(useToastStore().isError).toBe(true)
    expect(loads()).toBe(loadsBefore + 1)

    card(wrapper, 'Amy').vm.$emit('rename', 'Amelia')
    await flushPromises()
    expect(useToastStore().message).toBe('Failed to rename: Enter a name of up to 60 characters')
  })

  it('renames a person', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Brian').vm.$emit('rename', '  Bryan ')
    await flushPromises()
    expect(writes(fetchMock)).toEqual([['PATCH', '/api/ai/faces/people', { name: 'Brian', new_name: 'Bryan' }]])
    expect(useToastStore().message).toBe('Renamed to Bryan')
  })

  it('refuses an empty name, and ignores an unchanged one', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Brian').vm.$emit('rename', '   ')
    await flushPromises()
    expect(useToastStore().message).toBe('Name cannot be empty')
    card(wrapper, 'Brian').vm.$emit('rename', 'Brian')
    await flushPromises()
    expect(writes(fetchMock)).toEqual([])
  })

  it('asks before merging two people by renaming one onto the other', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Amy').vm.$emit('rename', 'Brian')
    await answer(false)
    expect(writes(fetchMock)).toEqual([])

    card(wrapper, 'Amy').vm.$emit('rename', 'Brian')
    await flushPromises()
    expect(useConfirmStore().message).toContain("Merge Amy's photos")
    await answer(true)
    expect(writes(fetchMock)).toEqual([['PATCH', '/api/ai/faces/people', { name: 'Amy', new_name: 'Brian' }]])
    expect(useToastStore().message).toBe('Merged into Brian')
  })

  it('removes a person after confirming', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Brian').vm.$emit('remove')
    await flushPromises()
    expect(useConfirmStore().message).toContain('Remove "Brian" (2 photos)')
    await answer(false)
    expect(writes(fetchMock)).toEqual([])

    card(wrapper, 'Amy').vm.$emit('remove')
    await flushPromises()
    expect(useConfirmStore().message).toContain('(1 photo)')
    await answer(true)
    expect(writes(fetchMock)).toEqual([['DELETE', '/api/ai/faces/people', { name: 'Amy' }]])
    expect(useToastStore().message).toBe('Removed Amy')
  })

  it("manages a person's photos, removing one after confirming", async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Brian').vm.$emit('manage')
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain("Brian's photos")

    body
      .findAll('button')
      .find((b) => b.text().includes('Remove photo'))!
      .trigger('click')
    await flushPromises()
    expect(useConfirmStore().message).toBe('Remove this photo of Brian? This cannot be undone.')
    await answer(true)
    expect(writes(fetchMock)).toEqual([['DELETE', '/api/ai/faces/1', undefined]])
    expect(useToastStore().message).toBe('Photo removed')
  })

  it("warns that removing someone's only photo removes them", async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    const removeOnly = async () => {
      card(wrapper, 'Amy').vm.$emit('manage')
      await flushPromises()
      new DOMWrapper(document.body)
        .findAll('button')
        .find((b) => b.text().includes('Remove photo'))!
        .trigger('click')
      await flushPromises()
    }
    await removeOnly()
    expect(useConfirmStore().message).toContain('only photo — removing it removes Amy entirely')
    await answer(false)
    expect(writes(fetchMock)).toEqual([])

    await removeOnly()
    await answer(true)
    expect(writes(fetchMock)).toEqual([['DELETE', '/api/ai/faces/3', undefined]])
    expect(useToastStore().message).toBe('Removed Amy')
  })

  it('closes the photos dialog', async () => {
    stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Amy').vm.$emit('manage')
    await flushPromises()
    new DOMWrapper(document.body)
      .findAll('button')
      .find((b) => b.text() === 'Close')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'PersonPhotosDialog' }).props('person')).toBeNull()
  })

  it('sends "add photos" to the face finder, from a card or the photos dialog', async () => {
    stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    card(wrapper, 'Amy').vm.$emit('add-photos')
    expect(addPhotosFor).toHaveBeenCalledWith('Amy')

    card(wrapper, 'Brian').vm.$emit('manage')
    await flushPromises()
    new DOMWrapper(document.body)
      .findAll('button')
      .find((b) => b.text().includes('Add photos'))!
      .trigger('click')
    await flushPromises()
    expect(addPhotosFor).toHaveBeenLastCalledWith('Brian')
    expect(wrapper.findComponent({ name: 'PersonPhotosDialog' }).props('person')).toBeNull()
  })

  it('does nothing with a photo removal when no dialog is open', async () => {
    const fetchMock = stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    wrapper.findComponent({ name: 'PersonPhotosDialog' }).vm.$emit('remove-photo', 1)
    await flushPromises()
    expect(writes(fetchMock)).toEqual([])
  })

  it('scans a clip reported as a missed match', async () => {
    stubFetch(() => facesResponse(PEOPLE))
    const wrapper = await mountPage()
    wrapper.findComponent(ActivityStub).vm.$emit('scan-clip', 'clip-9')
    expect(scanClip).toHaveBeenCalledWith('clip-9')
  })

  it('reloads after an enrollment and on a manual refresh', async () => {
    let faces = BRIAN
    const fetchMock = stubFetch(() => facesResponse(faces))
    const wrapper = await mountPage()
    faces = PEOPLE
    wrapper.findComponent(FinderStub).vm.$emit('enrolled')
    await flushPromises()
    expect(wrapper.findAllComponents(PersonCard)).toHaveLength(2)

    const loads = () => fetchMock.mock.calls.filter(([u]) => u === '/api/ai/faces').length
    const before = loads()
    useRefreshStore().bump()
    await flushPromises()
    expect(loads()).toBe(before + 1)
  })

  it('ignores a failure overtaken by a newer list', async () => {
    const slow = deferred<Response>()
    let calls = 0
    stubFetch(() => (++calls === 2 ? slow.promise : facesResponse(PEOPLE)))
    const wrapper = await mountPage()
    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    slow.reject(new TypeError('Failed to fetch'))
    await flushPromises()
    expect(wrapper.text()).not.toContain("Couldn't load enrolled people")
    expect(wrapper.findAllComponents(PersonCard)).toHaveLength(2)
  })

  it('ignores a list that arrives after a newer one', async () => {
    const slow = deferred<Response>()
    let calls = 0
    stubFetch(() => (++calls === 2 ? slow.promise : facesResponse(calls === 1 ? BRIAN : PEOPLE)))
    const wrapper = await mountPage()
    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    slow.resolve(jsonResponse(facesResponse([])))
    await flushPromises()
    expect(wrapper.findAllComponents(PersonCard)).toHaveLength(2)
  })
})
