import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Button from 'primevue/button'
import VehicleSignatureCard from './VehicleSignatureCard.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function routedFetch(signature: unknown, opts: { getFail?: boolean; deleteFail?: boolean } = {}) {
  return vi.fn((_url: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') {
      if (opts.deleteFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse({ reset: true }))
    }
    if (opts.getFail) return Promise.resolve(jsonResponse({}, false))
    return Promise.resolve(jsonResponse(signature))
  })
}

async function mountCard(signature: unknown, opts = {}) {
  vi.stubGlobal('fetch', routedFetch(signature, opts))
  const wrapper = mount(VehicleSignatureCard, { props: { camera: 'Driveway' } })
  await flushPromises()
  return wrapper
}

describe('VehicleSignatureCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('says it is still learning before anything is known', async () => {
    const wrapper = await mountCard({ camera: 'Driveway', learned: false, sample_count: 0 })
    expect(wrapper.text()).toContain('Still learning')
    expect(wrapper.findAllComponents(Button)).toHaveLength(0)
  })

  it('shows progress while the signature is not yet established', async () => {
    const wrapper = await mountCard({
      camera: 'Driveway',
      learned: true,
      established: false,
      sample_count: 2,
    })
    expect(wrapper.text()).toContain('2 of 4 confident sightings')
  })

  it('reports an established signature and offers a reset', async () => {
    const wrapper = await mountCard({
      camera: 'Driveway',
      learned: true,
      established: true,
      sample_count: 9,
    })
    expect(wrapper.text()).toContain('9 confident sightings')
    expect(wrapper.findAllComponents(Button)).toHaveLength(1)
  })

  it('renders nothing when the signature cannot be read', async () => {
    const wrapper = await mountCard({}, { getFail: true })
    expect(wrapper.find('[data-testid="vehicle-signature"]').exists()).toBe(false)
  })

  it('asks before forgetting what the camera learned', async () => {
    const wrapper = await mountCard({
      camera: 'Driveway',
      learned: true,
      established: true,
      sample_count: 9,
    })
    const confirm = useConfirmStore()
    const spy = vi.spyOn(confirm, 'ask').mockResolvedValue(false)
    await wrapper.findComponent(Button).trigger('click')
    await flushPromises()
    expect(spy).toHaveBeenCalled()
    expect(vi.mocked(fetch).mock.calls.some((c) => c[1]?.method === 'DELETE')).toBe(false)
  })

  it('clears the signature when confirmed', async () => {
    const wrapper = await mountCard({
      camera: 'Driveway',
      learned: true,
      established: true,
      sample_count: 9,
    })
    vi.spyOn(useConfirmStore(), 'ask').mockResolvedValue(true)
    await wrapper.findComponent(Button).trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some((c) => c[1]?.method === 'DELETE')).toBe(true)
    expect(useToastStore().message).toBe('Learned vehicle position cleared')
  })

  it('reports a failure to clear', async () => {
    const wrapper = await mountCard(
      { camera: 'Driveway', learned: true, established: true, sample_count: 9 },
      { deleteFail: true },
    )
    vi.spyOn(useConfirmStore(), 'ask').mockResolvedValue(true)
    await wrapper.findComponent(Button).trigger('click')
    await flushPromises()
    expect(useToastStore().isError).toBe(true)
  })

  it('ignores a slow answer for the camera the user has already left', async () => {
    // Nothing in the response says which camera it describes, so an
    // out-of-order resolve would render the previous camera's learned
    // signature under the new camera's name.
    const resolvers: ((value: Response) => void)[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve) => resolvers.push(resolve))),
    )
    const wrapper = mount(VehicleSignatureCard, { props: { camera: 'Driveway' } })
    await flushPromises()
    await wrapper.setProps({ camera: 'Back Yard' })
    await flushPromises()
    expect(resolvers).toHaveLength(2)

    resolvers[1](jsonResponse({ camera: 'Back Yard', learned: true, established: true, sample_count: 9 }))
    await flushPromises()
    resolvers[0](jsonResponse({ camera: 'Driveway', learned: false, sample_count: 0 }))
    await flushPromises()

    expect(wrapper.text()).toContain('9 confident sightings')
    expect(wrapper.text()).not.toContain('Still learning')
  })

  it('does not blank a loaded signature when an abandoned request fails afterwards', async () => {
    // The failure path needs the same token guard the success path has:
    // clearing `info` here would wipe the card for the camera the user is
    // actually looking at because a request they already left errored.
    const pending: { resolve: (v: Response) => void; reject: (e: Error) => void }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve, reject) => pending.push({ resolve, reject }))),
    )
    const wrapper = mount(VehicleSignatureCard, { props: { camera: 'Driveway' } })
    await flushPromises()
    await wrapper.setProps({ camera: 'Back Yard' })
    await flushPromises()

    pending[1].resolve(jsonResponse({ camera: 'Back Yard', learned: true, established: true, sample_count: 9 }))
    await flushPromises()
    pending[0].reject(new Error('down'))
    await flushPromises()

    expect(wrapper.text()).toContain('9 confident sightings')
  })

  it('clears the camera the dialog named, not whichever is selected by the time it is answered', async () => {
    // confirm.ask() is awaited, so the selection can move underneath it —
    // and resetting a camera the user never agreed to clear is worse than
    // not resetting at all.
    const wrapper = await mountCard({
      camera: 'Driveway',
      learned: true,
      established: true,
      sample_count: 9,
    })
    vi.spyOn(useConfirmStore(), 'ask').mockImplementation(async () => {
      await wrapper.setProps({ camera: 'Back Yard' })
      return true
    })
    await wrapper.findComponent(Button).trigger('click')
    await flushPromises()
    const deleted = vi.mocked(fetch).mock.calls.filter((c) => c[1]?.method === 'DELETE')
    expect(deleted).toHaveLength(1)
    expect(deleted[0][0]).toBe('/api/vehicle/signature/Driveway')
  })

  it('reloads when the camera changes', async () => {
    const wrapper = await mountCard({ camera: 'Driveway', learned: false, sample_count: 0 })
    await wrapper.setProps({ camera: 'Back Yard' })
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.map((c) => c[0])).toEqual([
      '/api/vehicle/signature/Driveway',
      '/api/vehicle/signature/Back%20Yard',
    ])
  })
})
