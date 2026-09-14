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
