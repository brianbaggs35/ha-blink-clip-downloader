import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import AutoComplete from 'primevue/autocomplete'
import FaceFinder from './FaceFinder.vue'
import FoundFaces from './FoundFaces.vue'
import { groupPeople } from './people'
import { useToastStore } from '../../stores/toast'
import type { ClipListItem, FaceCandidate } from '../../api/types'
import { candidate, clip, enrollment, errorResponse, jsonResponse } from './testing'

const scanSpy = vi.fn()
const ScannerStub = defineComponent({
  name: 'ClipFaceScanner',
  emits: ['found', 'scanning-change'],
  setup(_, { expose }) {
    expose({ scan: scanSpy })
    return {}
  },
  template: '<div class="scanner-stub" />',
})
const PhotoStub = defineComponent({
  name: 'PhotoFaceDetector',
  emits: ['found'],
  template: '<div class="photo-stub" />',
})

interface Routes {
  group?: (ids: string[]) => unknown
  enroll?: (body: Record<string, unknown>) => Promise<Response>
  clip?: () => Promise<Response>
}

function stubFetch(routes: Routes = {}) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(init.body as string) : {}
    if (url === '/api/ai/faces/group') {
      const result = routes.group ? routes.group(body.candidate_ids) : { groups: [body.candidate_ids], expired: [] }
      return result instanceof Error ? Promise.reject(result) : Promise.resolve(jsonResponse(result))
    }
    if (url === '/api/ai/faces') {
      return (
        routes.enroll?.(body) ??
        Promise.resolve(
          jsonResponse({
            name: body.name,
            enrolled: body.candidate_ids.length,
            expired: 0,
            approved: body.approved,
            existing: false,
          }),
        )
      )
    }
    if (url.startsWith('/api/clips/')) return routes.clip?.() ?? Promise.resolve(jsonResponse(clip('reported')))
    return Promise.reject(new Error(`unexpected fetch ${url}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const PEOPLE = groupPeople(
  [enrollment({ id: 1, name: 'Brian' }), enrollment({ id: 2, name: 'Nanny', approved: false })],
  640,
)

function mountFinder(available = true) {
  return mount(FaceFinder, {
    props: { available, people: PEOPLE },
    global: { stubs: { ClipFaceScanner: ScannerStub, PhotoFaceDetector: PhotoStub } },
  })
}

async function findFromClip(
  wrapper: ReturnType<typeof mountFinder>,
  faces: FaceCandidate[],
  from: ClipListItem = clip('c1'),
) {
  wrapper.findComponent(ScannerStub).vm.$emit('found', faces, from)
  await flushPromises()
}

async function select(wrapper: ReturnType<typeof mountFinder>, ids: string[]) {
  wrapper.findComponent(FoundFaces).vm.$emit('update:selected', ids)
  await flushPromises()
}

async function typeName(wrapper: ReturnType<typeof mountFinder>, name: string) {
  wrapper.findComponent(AutoComplete).vm.$emit('update:modelValue', name)
  await flushPromises()
}

function button(wrapper: ReturnType<typeof mountFinder>, label: string | RegExp) {
  return wrapper.findAll('button').find((b) => (typeof label === 'string' ? b.text() === label : label.test(b.text())))
}

describe('FaceFinder', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    scanSpy.mockReset()
    Element.prototype.scrollIntoView = vi.fn()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows faces found in clips and photos, grouped across both', async () => {
    const fetchMock = stubFetch()
    const wrapper = mountFinder()
    expect(wrapper.findComponent(FoundFaces).exists()).toBe(false)

    await findFromClip(wrapper, [candidate('a')])
    wrapper.findComponent(PhotoStub).vm.$emit('found', [candidate('b', { time: null })], 'me.jpg')
    await flushPromises()

    const found = wrapper.findComponent(FoundFaces)
    expect(found.props('faces').map((f: { id: string; source: { kind: string } }) => [f.id, f.source.kind])).toEqual([
      ['a', 'clip'],
      ['b', 'photo'],
    ])
    expect(found.props('groups')).toEqual([['a', 'b']])
    const groupCalls = fetchMock.mock.calls.filter(([u]) => u === '/api/ai/faces/group')
    expect(JSON.parse(groupCalls[1][1]!.body as string).candidate_ids).toEqual(['a', 'b'])
  })

  it('drops faces the server no longer holds', async () => {
    stubFetch({ group: (ids) => ({ groups: [ids.filter((i) => i !== 'old')], expired: ['old'] }) })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('old'), candidate('new')])
    expect(
      wrapper
        .findComponent(FoundFaces)
        .props('faces')
        .map((f: FaceCandidate) => f.id),
    ).toEqual(['new'])
  })

  it('unpicks a face the server no longer holds', async () => {
    let expire = false
    stubFetch({ group: (ids) => (expire ? { groups: [['keep']], expired: ['gone'] } : { groups: [ids], expired: [] }) })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('keep'), candidate('gone')])
    await select(wrapper, ['keep', 'gone'])
    expire = true
    await findFromClip(wrapper, [])
    expect(wrapper.findComponent(FoundFaces).props('selected')).toEqual(['keep'])
  })

  it('switches between finding faces in clips and in a photo', async () => {
    stubFetch()
    const wrapper = mountFinder()
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((t) => t.text())).toEqual(['From clips', 'From a photo'])
    await tabs[1].trigger('click')
    expect(wrapper.findAll('[role="tab"]')[1].attributes('aria-selected')).toBe('true')
  })

  it('still shows faces when grouping fails', async () => {
    stubFetch({ group: () => new Error('down') })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    expect(wrapper.findComponent(FoundFaces).props('faces')).toHaveLength(1)
    expect(wrapper.findComponent(FoundFaces).props('groups')).toEqual([])
  })

  it('ignores a grouping answer overtaken by a newer one', async () => {
    let release!: (value: Response) => void
    let calls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        calls++
        if (calls === 1) return new Promise<Response>((resolve) => (release = resolve))
        return Promise.resolve(jsonResponse({ groups: [['a'], ['b']], expired: [] }))
      }),
    )
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await findFromClip(wrapper, [candidate('b')])
    release(jsonResponse({ groups: [['a']], expired: ['b'] }))
    await flushPromises()
    expect(wrapper.findComponent(FoundFaces).props('groups')).toEqual([['a'], ['b']])
  })

  it('asks for a name once faces are picked, suggesting people already enrolled', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a'), candidate('b')])
    expect(wrapper.find('.enroll-bar').exists()).toBe(false)
    await select(wrapper, ['a', 'b'])
    expect(wrapper.find('.enroll-bar').text()).toContain('2 faces selected')

    const autocomplete = wrapper.findComponent(AutoComplete)
    autocomplete.vm.$emit('complete', { query: 'an', originalEvent: new Event('input') })
    await flushPromises()
    expect(autocomplete.props('suggestions')).toEqual(['Brian', 'Nanny'])
    autocomplete.vm.$emit('complete', { query: 'NAN', originalEvent: new Event('input') })
    await flushPromises()
    expect(autocomplete.props('suggestions')).toEqual(['Nanny'])
  })

  it('offers approval for a new person, and states it for an existing one', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    expect(wrapper.find('#biometrics-approve-new').exists()).toBe(true)

    await typeName(wrapper, 'Nanny')
    expect(wrapper.find('#biometrics-approve-new').exists()).toBe(false)
    expect(wrapper.find('.enroll-bar').text()).toContain('Adds to Nanny, who stays not approved')
    await typeName(wrapper, 'Brian')
    expect(wrapper.find('.enroll-bar').text()).toContain('who stays approved')
  })

  it('warns before filing a face recognized as someone else under another name', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a', { match: { name: 'Brian', similarity: 0.9 } }), candidate('b')])
    await select(wrapper, ['a', 'b'])
    expect(wrapper.text()).toContain('already recognized as Brian')
    expect(wrapper.text()).toContain('as someone else could make recognition confuse the two')

    await typeName(wrapper, 'Brian')
    expect(wrapper.text()).not.toContain('already recognized as')
  })

  it('points out picks that span more than one group', async () => {
    stubFetch({ group: () => ({ groups: [['a'], ['b']], expired: [] }) })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a'), candidate('b')])
    await select(wrapper, ['a'])
    expect(wrapper.text()).not.toContain('more than one group')
    await select(wrapper, ['a', 'b'])
    expect(wrapper.text()).toContain('more than one group')
  })

  it('enrolls the picked faces and clears them away', async () => {
    const fetchMock = stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a'), candidate('b'), candidate('c')])
    await select(wrapper, ['a', 'b'])
    await typeName(wrapper, '  Morgan ')
    wrapper.findComponent({ name: 'ToggleSwitch' }).vm.$emit('update:modelValue', false)
    await flushPromises()
    await button(wrapper, 'Enroll as Morgan')!.trigger('click')
    await flushPromises()

    const enrollCall = fetchMock.mock.calls.find(([u, init]) => u === '/api/ai/faces' && init?.method === 'POST')!
    expect(JSON.parse(enrollCall[1]!.body as string)).toEqual({
      name: 'Morgan',
      candidate_ids: ['a', 'b'],
      approved: false,
    })
    expect(useToastStore().message).toBe('Enrolled 2 photos of Morgan')
    expect(wrapper.emitted('enrolled')).toHaveLength(1)
    expect(
      wrapper
        .findComponent(FoundFaces)
        .props('faces')
        .map((f: FaceCandidate) => f.id),
    ).toEqual(['c'])
    expect(wrapper.find('.enroll-bar').exists()).toBe(false)

    await select(wrapper, ['c'])
    expect(wrapper.findComponent(AutoComplete).props('modelValue')).toBe('')
    expect(wrapper.find('#biometrics-approve-new').exists()).toBe(true)
  })

  it('says how many picked faces had expired', async () => {
    stubFetch({
      enroll: () =>
        Promise.resolve(jsonResponse({ name: 'Morgan', enrolled: 1, expired: 1, approved: true, existing: false })),
    })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a'), candidate('b')])
    await select(wrapper, ['a', 'b'])
    await typeName(wrapper, 'Morgan')
    await button(wrapper, 'Enroll as Morgan')!.trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('Enrolled 1 photo of Morgan — 1 had expired; scan again to add them')
  })

  it('reports an enrollment the server refused, and refreshes what it holds', async () => {
    const fetchMock = stubFetch({
      enroll: () =>
        Promise.resolve(jsonResponse({ error: 'Those faces are no longer available', enrolled: 0, expired: 1 })),
    })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    await typeName(wrapper, 'Morgan')
    const groupCallsBefore = fetchMock.mock.calls.filter(([u]) => u === '/api/ai/faces/group').length
    await button(wrapper, 'Enroll as Morgan')!.trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('Those faces are no longer available')
    expect(useToastStore().isError).toBe(true)
    expect(fetchMock.mock.calls.filter(([u]) => u === '/api/ai/faces/group').length).toBe(groupCallsBefore + 1)
    expect(wrapper.emitted('enrolled')).toBeUndefined()
  })

  it("reports a failed request, with the server's reason when it gave one", async () => {
    stubFetch({
      enroll: () =>
        Promise.resolve(errorResponse(400, JSON.stringify({ error: 'Enter a name of up to 60 characters' }))),
    })
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    await typeName(wrapper, 'Morgan')
    await button(wrapper, 'Enroll as Morgan')!.trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('Enrollment failed: Enter a name of up to 60 characters')

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await button(wrapper, 'Enroll as Morgan')!.trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('Enrollment failed: check your connection and try again')
  })

  it('refuses a name longer than the server accepts', async () => {
    const fetchMock = stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    await typeName(wrapper, 'x'.repeat(61))
    await button(wrapper, /Enroll as/)!.trigger('click')
    expect(useToastStore().message).toBe('Names can be up to 60 characters')
    expect(fetchMock.mock.calls.some(([u]) => u === '/api/ai/faces')).toBe(false)
  })

  it('needs a name before enrolling', async () => {
    const fetchMock = stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    await typeName(wrapper, '   ')
    await button(wrapper, 'Enroll')!.trigger('click')
    expect(useToastStore().message).toBe('Enter a name for the selected faces')
    expect(fetchMock.mock.calls.some(([u]) => u === '/api/ai/faces')).toBe(false)
  })

  it('adds a recognized group to that person in one step', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a'), candidate('b')])
    await select(wrapper, ['a'])
    wrapper.findComponent(FoundFaces).vm.$emit('add-to', 'Brian', ['a', 'b'])
    await flushPromises()
    expect(wrapper.findComponent(FoundFaces).props('selected')).toEqual(['a', 'b'])
    expect(wrapper.findComponent(AutoComplete).props('modelValue')).toBe('Brian')
  })

  it('clears found faces, and the selection with them', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    await button(wrapper, 'Clear selection')!.trigger('click')
    expect(wrapper.find('.enroll-bar').exists()).toBe(false)
    await select(wrapper, ['a'])
    await button(wrapper, 'Clear found faces')!.trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(FoundFaces).exists()).toBe(false)
    expect(wrapper.find('.enroll-bar').exists()).toBe(false)
  })

  it('cannot clear found faces mid-scan', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await findFromClip(wrapper, [candidate('a')])
    wrapper.findComponent(ScannerStub).vm.$emit('scanning-change', true)
    await flushPromises()
    expect(button(wrapper, 'Clear found faces')!.attributes('disabled')).toBeDefined()
  })

  it('scans a reported clip wherever it is in the library', async () => {
    stubFetch()
    const wrapper = mountFinder()
    await (wrapper.vm as unknown as { scanClip: (id: string) => Promise<void> }).scanClip('reported')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('/api/clips/reported', {})
    expect(scanSpy).toHaveBeenCalledWith([expect.objectContaining({ id: 'reported', face_recognized: false })])
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('says so when a reported clip has since been deleted, or cannot be loaded', async () => {
    stubFetch({ clip: () => Promise.resolve(errorResponse(404, 'Clip not found')) })
    const wrapper = mountFinder()
    const vm = wrapper.vm as unknown as { scanClip: (id: string) => Promise<void> }
    await vm.scanClip('gone')
    await flushPromises()
    expect(useToastStore().message).toBe('That clip is no longer in the library')

    stubFetch({ clip: () => Promise.reject(new TypeError('Failed to fetch')) })
    await vm.scanClip('gone')
    await flushPromises()
    expect(useToastStore().message).toBe("Couldn't open that clip — try again")
    expect(scanSpy).not.toHaveBeenCalled()
  })

  it('can be pointed at adding photos of someone', async () => {
    stubFetch()
    const wrapper = mountFinder()
    ;(wrapper.vm as unknown as { addPhotosFor: (name: string) => void }).addPhotosFor('Brian')
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    expect(wrapper.findComponent(AutoComplete).props('modelValue')).toBe('Brian')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('cannot enroll while face recognition is unavailable', async () => {
    stubFetch()
    const wrapper = mountFinder(false)
    await findFromClip(wrapper, [candidate('a')])
    await select(wrapper, ['a'])
    expect(button(wrapper, 'Enroll')!.attributes('disabled')).toBeDefined()
  })
})
