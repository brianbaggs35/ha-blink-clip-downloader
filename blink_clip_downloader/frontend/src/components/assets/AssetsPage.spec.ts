import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AssetsPage from './AssetsPage.vue'
import AssetCameraCard from './AssetCameraCard.vue'
import AssetEditorDialog from './AssetEditorDialog.vue'
import { activity, asset, jsonResponse } from './testing'
import type { AssetsResponse, CameraConfig, ProtectedAsset } from '../../api/types'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

function camera(name: string): CameraConfig {
  return { camera: name, description: '', custom_prompt: '', is_car_camera: false, car_zone: null }
}

// The user's own layout: two driveway cameras for the car (Vehicles tab),
// and assets marked on two of the other four.
const CAMERAS = ['Driveway 1', 'Driveway 2', 'Front Door', 'Back Door', 'Garage', 'Walkway'].map(camera)
const MAILBOX = asset({ id: 'mail1', camera: 'Front Door', name: 'Mailbox', asset_type: 'mailbox' })
const DOOR = asset({ id: 'door1', camera: 'Front Door', name: 'Front door', asset_type: 'door' })
const BBQ = asset({ id: 'bbq1', camera: 'Garage', name: 'Barbecue', asset_type: 'equipment' })

interface Routes {
  cameras?: CameraConfig[]
  assets?: Partial<AssetsResponse>
  fail?: (url: string, init?: RequestInit) => boolean
}

function stubFetch(routes: Routes = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (routes.fail?.(url, init)) return Promise.resolve(jsonResponse('Nope', 500))
      if (url === '/api/ai/camera-configs') return Promise.resolve(jsonResponse(routes.cameras ?? CAMERAS))
      if (url.startsWith('/api/assets/activity')) {
        return Promise.resolve(jsonResponse({ days: 7, activity: [activity({ camera: 'Front Door' })] }))
      }
      if (url === '/api/assets' && !init?.method) {
        return Promise.resolve(
          jsonResponse({
            assets: [MAILBOX, DOOR, BBQ].map((a) => ({ ...a })),
            limits: { per_camera: 12, name: 48, description: 160 },
            analysis_enabled: true,
            detection_enabled: true,
            ...routes.assets,
          }),
        )
      }
      if (url.startsWith('/api/assets/') && init?.method === 'PUT') {
        const id = url.split('/').pop()!
        const stored = [MAILBOX, DOOR, BBQ].find((a) => a.id === id)!
        return Promise.resolve(jsonResponse({ asset: { ...stored, ...JSON.parse(String(init.body)) } }))
      }
      if (url.startsWith('/api/assets/') && init?.method === 'DELETE') {
        return Promise.resolve(jsonResponse({ deleted: true }))
      }
      if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`unexpected fetch: ${url} ${init?.method ?? 'GET'}`))
    }),
  )
}

async function mountPage() {
  const wrapper = mount(AssetsPage, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

const card = (wrapper: VueWrapper, name: string) =>
  wrapper.findAllComponents(AssetCameraCard).find((c) => c.props('camera') === name)!

describe('AssetsPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('shows every camera, each with only its own assets', async () => {
    stubFetch()
    const wrapper = await mountPage()
    expect(wrapper.findAllComponents(AssetCameraCard).map((c) => c.props('camera'))).toEqual(
      CAMERAS.map((c) => c.camera),
    )
    expect(
      card(wrapper, 'Front Door')
        .props('assets')
        .map((a: ProtectedAsset) => a.name),
    ).toEqual(['Mailbox', 'Front door'])
    expect(
      card(wrapper, 'Garage')
        .props('assets')
        .map((a: ProtectedAsset) => a.name),
    ).toEqual(['Barbecue'])
    expect(card(wrapper, 'Walkway').props('assets')).toEqual([])
    expect(card(wrapper, 'Front Door').props('activity')).toHaveProperty('Front door')
    expect(card(wrapper, 'Garage').props('activity')).toEqual({})
    expect(card(wrapper, 'Garage').props('snapshotUrl')).toBe('/api/assets/snapshot/Garage?v=0')
    expect(wrapper.text()).toContain('Protecting 3 assets on 2 cameras.')
  })

  it('says one of each in the singular', async () => {
    stubFetch({ assets: { assets: [{ ...BBQ }] } })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('Protecting 1 asset on 1 camera.')
  })

  it('says when analysis is not running at all', async () => {
    stubFetch({ assets: { analysis_enabled: false, detection_enabled: false } })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain("AI analysis isn't set up")
    expect(wrapper.text()).not.toContain('Enhanced Detection')
  })

  it('says what object detection would add, once something is marked', async () => {
    stubFetch({ assets: { detection_enabled: false } })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('Enhanced Detection')

    stubFetch({ assets: { detection_enabled: false, assets: [] } })
    const empty = await mountPage()
    expect(empty.text()).not.toContain('Enhanced Detection')
    expect(empty.text()).not.toContain('Protecting')
  })

  it('explains how each kind of asset is watched', async () => {
    stubFetch()
    const wrapper = await mountPage()
    await wrapper.find('.how-panel button').trigger('click')
    expect(wrapper.text()).toContain('Package spot.')
    expect(wrapper.text()).toContain('approved household member')
  })

  it('shows the empty state with no cameras at all', async () => {
    stubFetch({ cameras: [], assets: { assets: [] } })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('No cameras found')
    expect(wrapper.findAllComponents(AssetCameraCard)).toHaveLength(0)
  })

  it('offers to try again when loading fails, keeping activity optional', async () => {
    let failing = true
    stubFetch({ fail: (url) => failing && url === '/api/assets' })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain("Couldn't load your assets.")
    failing = false
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Try again')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents(AssetCameraCard)).toHaveLength(6)

    stubFetch({ fail: (url) => url.startsWith('/api/assets/activity') })
    const noActivity = await mountPage()
    expect(card(noActivity, 'Front Door').props('activity')).toEqual({})
    expect(card(noActivity, 'Front Door').props('assets')).toHaveLength(2)
  })

  it("lists assets left on a camera that's gone, and lets them be removed", async () => {
    const orphan = asset({ id: 'old1', camera: 'Old Porch', name: 'Old door' })
    stubFetch({ assets: { assets: [{ ...BBQ }, orphan] } })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('Cameras no longer on your account')
    expect(wrapper.find('.orphaned').text()).toContain('Old door')
    expect(wrapper.find('.orphaned').text()).toContain('on Old Porch')
    // Not counted as protected: nothing analyzes a camera that isn't there.
    expect(wrapper.text()).toContain('Protecting 1 asset on 1 camera.')

    await wrapper.find('[aria-label="Remove Old door"]').trigger('click')
    useConfirmStore().settle(true)
    await flushPromises()
    expect(wrapper.find('.orphaned').exists()).toBe(false)
  })

  it('opens the editor to mark a new asset, and adds what it saves', async () => {
    stubFetch()
    const wrapper = await mountPage()
    card(wrapper, 'Walkway').vm.$emit('add')
    await flushPromises()
    const editor = wrapper.findComponent(AssetEditorDialog)
    expect(editor.props()).toMatchObject({
      visible: true,
      camera: 'Walkway',
      asset: null,
      others: [],
      hasReferenceFrame: false,
    })

    const saved = asset({ id: 'walk1', camera: 'Walkway', name: 'Planter', asset_type: 'other' })
    editor.vm.$emit('saved', saved)
    editor.vm.$emit('update:visible', false)
    await flushPromises()
    expect(card(wrapper, 'Walkway').props('assets')).toEqual([saved])
    // The frame is fetched afresh after a save.
    expect(card(wrapper, 'Walkway').props('snapshotUrl')).toBe('/api/assets/snapshot/Walkway?v=1')
    expect(useToastStore().message).toContain('“Planter” is now protected')
    expect(wrapper.findComponent(AssetEditorDialog).props('visible')).toBe(false)
  })

  it("opens the editor on an existing asset, with the camera's others shown beside it", async () => {
    stubFetch()
    const wrapper = await mountPage()
    card(wrapper, 'Front Door').vm.$emit('edit', card(wrapper, 'Front Door').props('assets')[1])
    await flushPromises()
    const editor = wrapper.findComponent(AssetEditorDialog)
    expect(editor.props('asset')?.id).toBe('door1')
    expect(editor.props('hasReferenceFrame')).toBe(true)
    expect(editor.props('others').map((o) => o.name)).toEqual(['Mailbox'])
    // The zone keeps its colour: second on the camera.
    expect(editor.props('color')).toBe('#f59e0b')

    editor.vm.$emit('saved', { ...DOOR, name: 'Porch door' })
    await flushPromises()
    expect(
      card(wrapper, 'Front Door')
        .props('assets')
        .map((a: ProtectedAsset) => a.name),
    ).toEqual(['Mailbox', 'Porch door'])
    expect(useToastStore().message).toContain('Saved “Porch door”')
  })

  it('switches an asset off and on, putting it back if the save fails', async () => {
    let failing = false
    stubFetch({ fail: (_url, init) => failing && init?.method === 'PUT' })
    const wrapper = await mountPage()
    const mailbox = () => card(wrapper, 'Front Door').props('assets')[0] as ProtectedAsset

    card(wrapper, 'Front Door').vm.$emit('toggle', mailbox(), false)
    await wrapper.vm.$nextTick()
    expect(card(wrapper, 'Front Door').props('busy').has('mail1')).toBe(true)
    await flushPromises()
    expect(mailbox().enabled).toBe(false)
    expect(card(wrapper, 'Front Door').props('busy').has('mail1')).toBe(false)
    expect(useToastStore().message).toContain('Stopped watching “Mailbox”')
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)![1]!.body))).toEqual({ enabled: false })

    card(wrapper, 'Front Door').vm.$emit('toggle', mailbox(), true)
    await flushPromises()
    expect(useToastStore().message).toContain('Watching “Mailbox” again')

    failing = true
    card(wrapper, 'Front Door').vm.$emit('toggle', mailbox(), false)
    await flushPromises()
    expect(mailbox().enabled).toBe(true)
    expect(useToastStore().message).toBe('Nope')
    expect(useToastStore().isError).toBe(true)
  })

  it('removes an asset only once confirmed, and says when removing fails', async () => {
    let failing = false
    stubFetch({ fail: (_url, init) => failing && init?.method === 'DELETE' })
    const wrapper = await mountPage()
    const confirm = useConfirmStore()

    card(wrapper, 'Garage').vm.$emit('remove', BBQ)
    await flushPromises()
    expect(confirm.message).toContain('Stop protecting “Barbecue” on Garage')
    confirm.settle(false)
    await flushPromises()
    expect(card(wrapper, 'Garage').props('assets')).toHaveLength(1)

    failing = true
    card(wrapper, 'Garage').vm.$emit('remove', BBQ)
    await flushPromises()
    confirm.settle(true)
    await flushPromises()
    expect(card(wrapper, 'Garage').props('assets')).toHaveLength(1)
    expect(useToastStore().isError).toBe(true)

    failing = false
    card(wrapper, 'Garage').vm.$emit('remove', BBQ)
    await flushPromises()
    confirm.settle(true)
    await flushPromises()
    expect(card(wrapper, 'Garage').props('assets')).toHaveLength(0)
    expect(useToastStore().message).toBe('Removed “Barbecue”')
  })

  it('reloads on a refresh, but never under an open editor', async () => {
    stubFetch()
    const wrapper = await mountPage()
    const loads = () => vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/assets').length
    useRefreshStore().bump()
    await flushPromises()
    expect(loads()).toBe(2)

    card(wrapper, 'Garage').vm.$emit('add')
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    expect(loads()).toBe(2)
    wrapper.findComponent(AssetEditorDialog).vm.$emit('update:visible', true)
    await flushPromises()
    expect(wrapper.findComponent(AssetEditorDialog).props('visible')).toBe(true)
  })

  it('applies only the newest load', async () => {
    const pending: ((r: Response) => void)[] = []
    stubFetch()
    const base = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((url, init) =>
      url === '/api/assets' && !init?.method ? new Promise((resolve) => pending.push(resolve)) : base(url, init),
    )
    const wrapper = mount(AssetsPage)
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    const body = (assets: ProtectedAsset[]) =>
      jsonResponse({
        assets,
        limits: { per_camera: 12, name: 48, description: 160 },
        analysis_enabled: true,
        detection_enabled: true,
      })
    pending[1](body([{ ...BBQ }]))
    await flushPromises()
    pending[0](body([{ ...MAILBOX }]))
    await flushPromises()
    expect(card(wrapper, 'Garage').props('assets')).toHaveLength(1)
    expect(card(wrapper, 'Front Door').props('assets')).toHaveLength(0)

    // A stale load's failure is ignored just the same.
    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    pending[3](body([{ ...BBQ }]))
    await flushPromises()
    pending[2](jsonResponse('late', 500))
    await flushPromises()
    expect(wrapper.text()).not.toContain("Couldn't load your assets.")
  })

  it('renders its content inside the page body for the dialog to reach', async () => {
    stubFetch()
    await mountPage()
    expect(new DOMWrapper(document.body).find('.assets-page h2').text()).toBe('Assets')
  })
})
