import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import AssetEditorDialog from './AssetEditorDialog.vue'
import AssetZoneCanvas from './AssetZoneCanvas.vue'
import { asset, clip, jsonResponse } from './testing'
import type { AssetZone, ProtectedAsset } from '../../api/types'

const ZONE: AssetZone = { shape: 'rect', x_min: 0.2, y_min: 0.3, x_max: 0.4, y_max: 0.7 }
const LIMITS = { per_camera: 12, name: 48, description: 160 }

type Route = (url: string, init?: RequestInit) => Promise<Response> | undefined

function stubFetch(route: Route = () => undefined) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const routed = route(url, init)
      if (routed) return routed
      if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([clip('c1'), clip('c2')]))
      if (url === '/api/assets' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        return Promise.resolve(jsonResponse({ asset: asset({ id: 'new1', ...body }) }))
      }
      if (url.startsWith('/api/assets/') && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body))
        return Promise.resolve(jsonResponse({ asset: asset(body) }))
      }
      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    }),
  )
}

async function mountEditor(props: Partial<InstanceType<typeof AssetEditorDialog>['$props']> = {}) {
  const wrapper = mount(AssetEditorDialog, {
    props: {
      visible: true,
      camera: 'Porch',
      asset: null,
      others: [],
      color: '#22d3ee',
      hasReferenceFrame: false,
      snapshotUrl: '/api/assets/snapshot/Porch?v=0',
      limits: LIMITS,
      ...props,
    },
  })
  await flushPromises()
  return wrapper
}

const body = () => new DOMWrapper(document.body)
const button = (label: string) =>
  body()
    .findAll('button')
    .find((b) => b.text().includes(label))!
const nameInput = () => body().find<HTMLInputElement>('input[placeholder="e.g. Front door"]')
const calls = (method: string) =>
  vi.mocked(fetch).mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === method)
const sentBody = (method: string) => JSON.parse(String((calls(method)[0][1] as RequestInit).body))

async function fill(wrapper: VueWrapper, type = 'door', zone: AssetZone | null = ZONE) {
  wrapper.findComponent(Select).vm.$emit('update:modelValue', type)
  await flushPromises()
  if (zone) wrapper.findComponent(AssetZoneCanvas).vm.$emit('update:modelValue', zone)
  await flushPromises()
}

describe('AssetEditorDialog', () => {
  beforeEach(() => stubFetch())
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it("marks a camera's first asset on its newest clip's frame", async () => {
    const wrapper = await mountEditor()
    expect(body().text()).toContain('Mark an asset on Porch')
    const canvas = wrapper.findComponent(AssetZoneCanvas)
    expect(canvas.props('src')).toBe('/api/clips/c1/thumb')
    // No saved frame yet, so none is offered.
    expect(body().text()).not.toContain('Saved')

    await fill(wrapper)
    expect(nameInput().element.value).toBe('Front door')
    expect(body().text()).toContain('Knocking, ringing and deliveries are routine')
    await button('Save asset').trigger('click')
    await flushPromises()

    expect(sentBody('POST')).toEqual({
      camera: 'Porch',
      name: 'Front door',
      asset_type: 'door',
      description: '',
      zone: ZONE,
      clip_id: 'c1',
    })
    expect((wrapper.emitted('saved')![0][0] as ProtectedAsset).id).toBe('new1')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('offers each type its usual name until one is typed', async () => {
    const wrapper = await mountEditor()
    await fill(wrapper, 'door', null)
    await fill(wrapper, 'gate', null)
    expect(nameInput().element.value).toBe('Gate')
    await nameInput().setValue('Side gate')
    await fill(wrapper, 'mailbox', null)
    expect(nameInput().element.value).toBe('Side gate')
    // "Something else" has no usual name to offer.
    await nameInput().setValue('')
    await fill(wrapper, 'other', null)
    expect(nameInput().element.value).toBe('')
  })

  it('says what is still missing, and names an empty name once it is left', async () => {
    const wrapper = await mountEditor()
    expect(body().text()).toContain('To save, choose what it is, then name it, then draw it on the frame.')
    expect(button('Save asset').attributes('disabled')).toBeDefined()
    await nameInput().trigger('blur')
    expect(body().text()).toContain('Give it a name.')
    await fill(wrapper, 'bicycle')
    await nameInput().setValue('   ')
    expect(button('Save asset').attributes('disabled')).toBeDefined()
    await nameInput().setValue(' Kids’ bikes ')
    expect(body().text()).not.toContain('To save,')
    await button('Save asset').trigger('click')
    await flushPromises()
    expect(sentBody('POST').name).toBe('Kids’ bikes')
  })

  it('shows the frames loading, then says when a camera has none yet', async () => {
    let resolve!: (r: Response) => void
    stubFetch((url) => (url.startsWith('/api/clips') ? new Promise((r) => (resolve = r)) : undefined))
    const wrapper = await mountEditor()
    expect(body().text()).toContain("Loading this camera's recent frames")
    resolve(jsonResponse([]))
    await flushPromises()
    expect(body().text()).toContain('No clips from this camera yet')
    expect(wrapper.findComponent(AssetZoneCanvas).exists()).toBe(false)
  })

  it('offers to try again when the frames fail to load', async () => {
    let fail = true
    stubFetch((url) => {
      if (!url.startsWith('/api/clips')) return undefined
      return fail ? Promise.resolve(jsonResponse('boom', 500)) : Promise.resolve(jsonResponse([clip('c9')]))
    })
    const wrapper = await mountEditor()
    expect(body().text()).toContain("Couldn't load this camera's recent clips")
    fail = false
    await button('Try again').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/clips/c9/thumb')
  })

  it("edits an asset over the camera's saved frame, sending no clip", async () => {
    const bike = asset({
      id: 'bike1',
      name: 'Bike',
      asset_type: 'bicycle',
      description: 'Red',
      zone: {
        shape: 'polygon',
        points: [
          [0.1, 0.1],
          [0.3, 0.1],
          [0.2, 0.4],
        ],
      },
    })
    const wrapper = await mountEditor({ asset: bike, hasReferenceFrame: true })
    expect(body().text()).toContain('Edit “Bike”')
    expect(nameInput().element.value).toBe('Bike')
    expect(wrapper.findComponent(SelectButton).props('modelValue')).toBe('polygon')
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/assets/snapshot/Porch?v=0')
    expect(body().findAll('.frame-tile')).toHaveLength(3)

    await nameInput().setValue('Blue bike')
    await button('Save changes').trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/assets/bike1')).toBe(true)
    expect(sentBody('PUT')).toEqual({ name: 'Blue bike', asset_type: 'bicycle', description: 'Red', zone: bike.zone })
  })

  it('redraws on a newer frame, which then becomes the saved one', async () => {
    const wrapper = await mountEditor({ asset: asset(), hasReferenceFrame: true })
    const tiles = body().findAll('.frame-tile')
    expect(tiles[0].attributes('aria-pressed')).toBe('true')
    await tiles[2].trigger('click')
    expect(tiles[2].attributes('aria-pressed')).toBe('true')
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/clips/c2/thumb')
    await button('Save changes').trigger('click')
    await flushPromises()
    expect(sentBody('PUT').clip_id).toBe('c2')
    // Back to the saved frame.
    await tiles[0].trigger('click')
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/assets/snapshot/Porch?v=0')
  })

  it("marks another asset on the camera's saved frame by default", async () => {
    const wrapper = await mountEditor({ hasReferenceFrame: true })
    await fill(wrapper)
    await button('Save asset').trigger('click')
    await flushPromises()
    expect(sentBody('POST')).not.toHaveProperty('clip_id')
  })

  it("shows the server's reason when a save is refused", async () => {
    stubFetch((url, init) =>
      url === '/api/assets' && init?.method === 'POST'
        ? Promise.resolve(jsonResponse('This camera already has an asset called “Front door”', 409))
        : undefined,
    )
    const wrapper = await mountEditor()
    await fill(wrapper)
    await button('Save asset').trigger('click')
    await flushPromises()
    expect(body().text()).toContain('This camera already has an asset called “Front door”')
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(wrapper.emitted('update:visible')).toBeUndefined()
  })

  it('clears the drawn zone', async () => {
    const wrapper = await mountEditor({ asset: asset(), hasReferenceFrame: true })
    await button('Clear').trigger('click')
    expect(wrapper.findComponent(AssetZoneCanvas).props('modelValue')).toBeNull()
    expect(button('Save changes').attributes('disabled')).toBeDefined()
  })

  it("says when a frame won't load, and when a strip thumbnail is missing", async () => {
    const wrapper = await mountEditor()
    wrapper.findComponent(AssetZoneCanvas).vm.$emit('error')
    await flushPromises()
    expect(body().text()).toContain("This frame couldn't be loaded")
    wrapper.findComponent(AssetZoneCanvas).vm.$emit('load')
    await flushPromises()
    expect(body().text()).not.toContain("This frame couldn't be loaded")

    await body().findAll('.frame-tile img')[0].trigger('error')
    expect(body().findAll('.frame-tile-missing')).toHaveLength(1)
  })

  it('cancels', async () => {
    const wrapper = await mountEditor()
    await button('Cancel').trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('passes the dialog closing itself up', async () => {
    const wrapper = await mountEditor()
    await body().find('.p-dialog-close-button').trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it("applies only the newest camera's frames", async () => {
    const pending: Record<string, (r: Response) => void> = {}
    stubFetch((url) => {
      if (!url.startsWith('/api/clips')) return undefined
      const camera = new URL(url, 'http://x').searchParams.get('camera')!
      return new Promise((r) => (pending[camera] = r))
    })
    const wrapper = await mountEditor({ camera: 'Porch' })
    await wrapper.setProps({ visible: false })
    await wrapper.setProps({ camera: 'Yard', visible: true })
    await flushPromises()
    pending.Yard(jsonResponse([clip('y1', 'Yard')]))
    await flushPromises()
    pending.Porch(jsonResponse([clip('p1')]))
    await flushPromises()
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/clips/y1/thumb')
  })

  it('opens fresh each time', async () => {
    const wrapper = await mountEditor()
    await fill(wrapper)
    await wrapper.setProps({ visible: false })
    await wrapper.setProps({ visible: true })
    await flushPromises()
    expect(nameInput().element.value).toBe('')
    expect(wrapper.findComponent(AssetZoneCanvas).props('modelValue')).toBeNull()
  })

  it('lists every kind of asset, each with its icon, and keeps a description', async () => {
    const wrapper = await mountEditor()
    await body().find('.p-select').trigger('click')
    await flushPromises()
    const options = body().findAll('.p-select-option')
    expect(options).toHaveLength(9)
    expect(options[4].text()).toBe('Package spot')
    expect(options[4].find('.pi-box').exists()).toBe(true)
    await options[4].trigger('mousedown')
    await flushPromises()
    expect(body().find('.p-select-label').text()).toBe('Package spot')

    await body().find('textarea').setValue('  Blue step by the door  ')
    wrapper.findComponent(AssetZoneCanvas).vm.$emit('update:modelValue', ZONE)
    await flushPromises()
    await button('Save asset').trigger('click')
    await flushPromises()
    expect(sentBody('POST')).toMatchObject({ asset_type: 'package_area', description: 'Blue step by the door' })
  })

  it('saves on Enter in the name, once, and only when the form is complete', async () => {
    const wrapper = await mountEditor()
    await nameInput().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(calls('POST')).toHaveLength(0)
    expect(body().text()).toContain('Give it a name.')

    await fill(wrapper)
    await nameInput().trigger('keydown', { key: 'Enter' })
    await nameInput().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(calls('POST')).toHaveLength(1)
  })

  it("ignores a slower camera's failure to load", async () => {
    const pending: Record<string, (r: Response) => void> = {}
    stubFetch((url) => {
      if (!url.startsWith('/api/clips')) return undefined
      const camera = new URL(url, 'http://x').searchParams.get('camera')!
      return new Promise((r) => (pending[camera] = r))
    })
    const wrapper = await mountEditor({ camera: 'Porch' })
    await wrapper.setProps({ visible: false })
    await wrapper.setProps({ camera: 'Yard', visible: true })
    await flushPromises()
    pending.Yard(jsonResponse([clip('y1', 'Yard')]))
    await flushPromises()
    pending.Porch(jsonResponse('boom', 500))
    await flushPromises()
    expect(body().text()).not.toContain("Couldn't load this camera's recent clips")
    expect(wrapper.findComponent(AssetZoneCanvas).props('src')).toBe('/api/clips/y1/thumb')
  })

  it('switches drawing tool', async () => {
    const wrapper = await mountEditor()
    wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'polygon')
    await flushPromises()
    expect(wrapper.findComponent(AssetZoneCanvas).props('tool')).toBe('polygon')
    expect(body().text()).toContain('Press, trace around it')
  })
})
