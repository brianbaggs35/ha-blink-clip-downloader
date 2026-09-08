import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import SyncModulePage from './SyncModulePage.vue'
import SyncModuleCard from './SyncModuleCard.vue'
import SyncModuleCameraCard from './SyncModuleCameraCard.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import type { SyncModuleInfo } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function makeModule(overrides: Partial<SyncModuleInfo> = {}): SyncModuleInfo {
  return {
    name: 'Home',
    network_id: 12345,
    serial: 'ABCDEF123',
    version: '2.13.30',
    status: 'online',
    online: true,
    armed: true,
    region_id: 'u001',
    local_storage: false,
    cameras: [
      {
        name: 'Front Door',
        armed: true,
        online: true,
        battery_state: 'ok',
        battery_level: 3,
        wifi_strength: -55,
        type: 'catalina',
      },
      {
        name: 'Backyard',
        armed: true,
        online: true,
        battery_state: 'low',
        battery_level: 1,
        wifi_strength: -60,
        type: 'catalina',
      },
    ],
    ...overrides,
  }
}

function routedFetch(extra: (url: string, init?: RequestInit) => Promise<Response> | undefined) {
  return vi.fn(
    (url: string, init?: RequestInit) => extra(url, init) ?? Promise.reject(new Error(`unhandled fetch: ${url}`)),
  )
}

function mountPage() {
  return mount(SyncModulePage, { global: { plugins: [PrimeVue] } })
}

describe('SyncModulePage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows a loading indicator, then the loaded content', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule()]))),
    )
    const wrapper = mountPage()
    expect(wrapper.findComponent({ name: 'LoadingIndicator' }).exists()).toBe(true)
    await flushPromises()
    expect(wrapper.findComponent({ name: 'LoadingIndicator' }).exists()).toBe(false)
    expect(wrapper.text()).toContain('Home')
  })

  it('shows a load error message when the request fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load sync modules')
  })

  it('shows an empty state when there are no sync modules', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('No sync modules found')
  })

  it('renders one SyncModuleCard per sync module, and the camera-count subtitle', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule(), makeModule({ name: 'Garage', cameras: [] })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAllComponents(SyncModuleCard)).toHaveLength(2)
    expect(wrapper.text()).toContain('2 of 2 cameras armed')
  })

  it('shows "System Armed" when every sync module is armed', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule({ armed: true })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-armed')
  })

  it('shows "Disarmed" when every sync module is disarmed', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule({ armed: false })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-disarmed')
  })

  it('shows "Partially Armed", not "Disarmed", when a module\'s armed state is unknown (null)', async () => {
    // blinkpy's own `arm` property returns null (not false) whenever
    // network_info hasn't been populated/parsed yet -- a module in that
    // state must not be reported as confirmed-disarmed, which would
    // falsely tell the user their home is unprotected.
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule({ armed: null })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Partially Armed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-mixed')
  })

  it('does not count a disarmed module\'s cameras toward "armed" in the subtitle, even though their own motion-detection flag is untouched', async () => {
    // A disarmed module stops every one of its cameras from recording
    // regardless of their own armed flag -- makeModule()'s default cameras
    // are both armed: true, so a subtitle that ignored module state here
    // would contradict the "Disarmed" headline right above it.
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule({ armed: false })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('0 of 2 cameras armed')
  })

  it('shows "Partially Armed" when sync modules disagree', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() =>
        Promise.resolve(
          jsonResponse([makeModule({ name: 'Home', armed: true }), makeModule({ name: 'Garage', armed: false })]),
        ),
      ),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Partially Armed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-mixed')
  })

  it('shows "Partially Armed" when a module is armed but one of its cameras is disarmed, and back to "System Armed" once re-armed', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          const armed = (JSON.parse(init.body as string) as { armed: boolean }).armed
          return Promise.resolve(jsonResponse({ armed }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule()]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', false)
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Partially Armed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-mixed')

    wrapper.findAllComponents(SyncModuleCameraCard)[0]!.vm.$emit('update:armed', true)
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-armed')
  })

  it("a disarmed sync module counts as fully disarmed regardless of its cameras' own armed state", async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() =>
        Promise.resolve(
          jsonResponse([
            makeModule({
              armed: false,
              cameras: [{ ...makeModule().cameras[0]!, armed: true }, makeModule().cameras[1]!],
            }),
          ]),
        ),
      ),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')
    expect(wrapper.find('.system-hero').classes()).toContain('system-hero-disarmed')
  })

  it('arms the entire system via the hero button, reloads, and shows a success toast', async () => {
    const armCalls: string[] = []
    const routes: { armed: boolean } = { armed: false }
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          armCalls.push(JSON.parse(init.body as string).armed ? 'arm' : 'disarm')
          routes.armed = true
          return Promise.resolve(jsonResponse({ armed: true }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: routes.armed })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')

    await wrapper.find('.system-hero-btn').trigger('click')
    await flushPromises()

    expect(armCalls).toEqual(['arm'])
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')
    const toast = useToastStore()
    expect(toast.message).toBe('Entire system armed')
    expect(toast.isError).toBe(false)
  })

  it('shows a failure toast naming the sync modules that failed to arm', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Failing/arm' && init?.method === 'POST') return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST')
          return Promise.resolve(jsonResponse({ armed: true }))
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([makeModule({ name: 'Home', armed: false }), makeModule({ name: 'Failing', armed: false })]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('.system-hero-btn').trigger('click')
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Could not arm: Failing')
    expect(toast.isError).toBe(true)
  })

  it('shows a failure toast naming the sync modules that failed to disarm', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Failing/arm' && init?.method === 'POST') return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST')
          return Promise.resolve(jsonResponse({ armed: false }))
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([makeModule({ name: 'Home', armed: true }), makeModule({ name: 'Failing', armed: true })]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('.system-hero-btn').trigger('click')
    await flushPromises()
    useConfirmStore().settle(true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Could not disarm: Failing')
    expect(toast.isError).toBe(true)
  })

  it('toggling the only (last-armed) sync module asks for confirmation, then updates it and shows a toast', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST')
          return Promise.resolve(jsonResponse({ armed: false }))
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: true })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(SyncModuleCard).vm.$emit('toggle-module', false)
    await flushPromises()
    const confirm = useConfirmStore()
    expect(confirm.open).toBe(true)
    confirm.settle(true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Home disarmed')
    expect(wrapper.findComponent(SyncModuleCard).props('module').armed).toBe(false)
  })

  it('does nothing if the confirmation for disarming the last-armed module is declined', async () => {
    const armCalls: unknown[] = []
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          armCalls.push(init)
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: true })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(SyncModuleCard).vm.$emit('toggle-module', false)
    await flushPromises()
    useConfirmStore().settle(false)
    await flushPromises()

    expect(armCalls).toHaveLength(0)
    expect(wrapper.findComponent(SyncModuleCard).props('module').armed).toBe(true)
  })

  it('does not ask for confirmation when disarming one module while another stays armed', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST')
          return Promise.resolve(jsonResponse({ armed: false }))
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([makeModule({ name: 'Home', armed: true }), makeModule({ name: 'Garage', armed: true })]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findAllComponents(SyncModuleCard)[0]!.vm.$emit('toggle-module', false)
    await flushPromises()

    expect(useConfirmStore().open).toBe(false)
    const toast = useToastStore()
    expect(toast.message).toBe('Home disarmed')
  })

  it('shows a failure toast and leaves state unchanged when disarming a module fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: true })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(SyncModuleCard).vm.$emit('toggle-module', false)
    await flushPromises()
    useConfirmStore().settle(true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Failed to disarm Home')
    expect(toast.isError).toBe(true)
    expect(wrapper.findComponent(SyncModuleCard).props('module').armed).toBe(true)
  })

  it('arming a disarmed module needs no confirmation, updates it, and shows a toast', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST')
          return Promise.resolve(jsonResponse({ armed: true }))
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: false })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(SyncModuleCard).vm.$emit('toggle-module', true)
    await flushPromises()

    expect(useConfirmStore().open).toBe(false)
    const toast = useToastStore()
    expect(toast.message).toBe('Home armed')
    expect(wrapper.findComponent(SyncModuleCard).props('module').armed).toBe(true)
  })

  it('shows a failure toast and leaves state unchanged when arming a module fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: false })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(SyncModuleCard).vm.$emit('toggle-module', true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Failed to arm Home')
    expect(toast.isError).toBe(true)
    expect(wrapper.findComponent(SyncModuleCard).props('module').armed).toBe(false)
  })

  it('requires confirmation before disarming the entire system via the hero button, and does nothing if declined', async () => {
    const armCalls: unknown[] = []
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          armCalls.push(init)
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: true })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')

    await wrapper.find('.system-hero-btn').trigger('click')
    await flushPromises()
    const confirm = useConfirmStore()
    expect(confirm.open).toBe(true)
    confirm.settle(false)
    await flushPromises()

    expect(armCalls).toHaveLength(0)
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')
  })

  it('disarms the entire system via the hero button once the confirmation is accepted', async () => {
    const routes = { armed: true }
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          routes.armed = false
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: routes.armed })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')

    await wrapper.find('.system-hero-btn').trigger('click')
    await flushPromises()
    useConfirmStore().settle(true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Entire system disarmed')
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')
  })

  it('shows an in-flight "Arming…" label and disables the hero button until the request settles', async () => {
    let resolveArm: (() => void) | undefined
    const routes = { armed: false }
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          return new Promise((resolve) => {
            resolveArm = () => {
              routes.armed = true
              resolve(jsonResponse({ armed: true }))
            }
          })
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: routes.armed })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const btn = wrapper.find('.system-hero-btn')
    expect(btn.text()).toContain('Arm Entire System')
    expect(btn.attributes('disabled')).toBeUndefined()

    await btn.trigger('click')
    await flushPromises()

    expect(btn.text()).toContain('Arming…')
    expect(btn.attributes('disabled')).toBeDefined()

    resolveArm?.()
    await flushPromises()
    expect(btn.text()).toContain('Disarm Entire System')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('shows an in-flight "Disarming…" label on the hero button once the disarm is confirmed', async () => {
    let resolveArm: (() => void) | undefined
    const routes = { armed: true }
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          return new Promise((resolve) => {
            resolveArm = () => {
              routes.armed = false
              resolve(jsonResponse({ armed: false }))
            }
          })
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: routes.armed })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const btn = wrapper.find('.system-hero-btn')

    await btn.trigger('click')
    await flushPromises()
    useConfirmStore().settle(true)
    await flushPromises()

    expect(btn.text()).toContain('Disarming…')
    expect(btn.attributes('disabled')).toBeDefined()

    resolveArm?.()
    await flushPromises()
    expect(btn.text()).toContain('Arm Entire System')
  })

  it('disarming a camera updates it wherever it appears and shows a toast', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule()]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', false)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Front Door disarmed')
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('camera').armed).toBe(false)
  })

  it('arming a disarmed camera updates it and shows a toast', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          return Promise.resolve(jsonResponse({ armed: true }))
        }
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([
              makeModule({ cameras: [{ ...makeModule().cameras[0]!, armed: false }, makeModule().cameras[1]!] }),
            ]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Front Door armed')
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('camera').armed).toBe(true)
  })

  it('updates a camera only on the sync module that actually owns it, across multiple modules', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([
              makeModule({ name: 'Home' }),
              makeModule({
                name: 'Garage',
                cameras: [
                  {
                    name: 'Side Door',
                    armed: true,
                    online: true,
                    battery_state: 'ok',
                    battery_level: 3,
                    wifi_strength: -55,
                    type: 'catalina',
                  },
                ],
              }),
            ]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const homeFrontDoor = wrapper
      .findAllComponents(SyncModuleCameraCard)
      .find((c) => c.props('camera').name === 'Front Door')!
    homeFrontDoor.vm.$emit('update:armed', false)
    await flushPromises()

    expect(useToastStore().message).toBe('Front Door disarmed')
    const garageSideDoor = wrapper
      .findAllComponents(SyncModuleCameraCard)
      .find((c) => c.props('camera').name === 'Side Door')!
    expect(garageSideDoor.props('camera').armed).toBe(true)
  })

  it('shows a failure toast and leaves state unchanged when arming a camera fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST')
          return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules')
          return Promise.resolve(
            jsonResponse([
              makeModule({ cameras: [{ ...makeModule().cameras[0]!, armed: false }, makeModule().cameras[1]!] }),
            ]),
          )
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', true)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Failed to arm Front Door')
    expect(toast.isError).toBe(true)
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('camera').armed).toBe(false)
  })

  it('shows a failure toast and leaves state unchanged when disarming a camera fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST')
          return Promise.reject(new Error('down'))
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule()]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', false)
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Failed to disarm Front Door')
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('camera').armed).toBe(true)
  })

  it('marks a camera as pending while its own toggle request is in flight, without blocking others', async () => {
    let resolveArm: (() => void) | undefined
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          return new Promise((resolve) => {
            resolveArm = () => resolve(jsonResponse({ armed: false }))
          })
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule()]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const [frontDoorCard, backyardCard] = wrapper.findAllComponents(SyncModuleCameraCard)
    frontDoorCard!.vm.$emit('update:armed', false)
    await flushPromises()

    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('pending')).toBe(true)
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[1]!.props('pending')).toBe(false)
    expect(backyardCard).toBeDefined()

    resolveArm?.()
    await flushPromises()
    expect(wrapper.findAllComponents(SyncModuleCameraCard)[0]!.props('pending')).toBe(false)
  })

  // The ToggleSwitch/Button are already :disabled while pending, which
  // blocks a *real* second click once Vue has patched that attribute into
  // the DOM -- these three tests instead fire both requests back-to-back
  // with no await between them, so both reach the handler before that
  // patch lands, exercising the explicit re-entrancy guard itself as the
  // actual last line of defense against a double-click race.
  it('a second hero-button click fired before the first re-render lands is ignored', async () => {
    let armCallCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          armCallCount++
          return Promise.resolve(jsonResponse({ armed: true }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: false })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const btn = wrapper.find('.system-hero-btn')
    await Promise.all([btn.trigger('click'), btn.trigger('click')])
    await flushPromises()

    expect(armCallCount).toBe(1)
  })

  it('a second toggle on the same sync module fired before the first re-render lands is ignored', async () => {
    let armCallCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/Home/arm' && init?.method === 'POST') {
          armCallCount++
          return Promise.resolve(jsonResponse({ armed: true }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule({ armed: false })]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const card = wrapper.findComponent(SyncModuleCard)
    card.vm.$emit('toggle-module', true)
    card.vm.$emit('toggle-module', true)
    await flushPromises()

    expect(armCallCount).toBe(1)
  })

  it('a second toggle on the same camera fired before the first re-render lands is ignored', async () => {
    let armCallCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        if (url === '/api/sync-modules/cameras/Front%20Door/arm' && init?.method === 'POST') {
          armCallCount++
          return Promise.resolve(jsonResponse({ armed: false }))
        }
        if (url === '/api/sync-modules') return Promise.resolve(jsonResponse([makeModule()]))
        return undefined
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const cameraCard = wrapper.findAllComponents(SyncModuleCameraCard)[0]!
    cameraCard.vm.$emit('update:armed', false)
    cameraCard.vm.$emit('update:armed', false)
    await flushPromises()

    expect(armCallCount).toBe(1)
  })

  it('silently reloads on a shared refresh tick without showing the full-page loading state', async () => {
    let reads = 0
    vi.stubGlobal(
      'fetch',
      routedFetch(() => {
        reads++
        return Promise.resolve(jsonResponse([makeModule({ armed: reads > 1 })]))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')

    useRefreshStore().bump()
    await flushPromises()

    expect(wrapper.findComponent({ name: 'LoadingIndicator' }).exists()).toBe(false)
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')
  })

  it('polls for updates every 30s while mounted, and stops polling on unmount', async () => {
    let reads = 0
    vi.stubGlobal(
      'fetch',
      routedFetch(() => {
        reads++
        return Promise.resolve(jsonResponse([makeModule({ armed: reads <= 1 })]))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('System Armed')

    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()
    expect(wrapper.find('.system-hero-title').text()).toBe('Disarmed')

    const readsBeforeUnmount = reads
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)
    await flushPromises()
    expect(reads).toBe(readsBeforeUnmount)
  })

  it('ignores a stale silent-reload response that resolves after a newer one already landed', async () => {
    let resolveSecondCall: ((r: Response) => void) | undefined
    let callCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch(() => {
        callCount++
        if (callCount === 1) return Promise.resolve(jsonResponse([makeModule({ name: 'Initial' })]))
        if (callCount === 2) {
          return new Promise((resolve) => {
            resolveSecondCall = resolve
          })
        }
        return Promise.resolve(jsonResponse([makeModule({ name: 'Newer' })]))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Initial')

    // Two overlapping refresh ticks: the first silent reload (2nd fetch
    // overall) hangs, the second (3rd fetch) resolves immediately.
    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.text()).toContain('Newer')

    resolveSecondCall?.(jsonResponse([makeModule({ name: 'Stale' })]))
    await flushPromises()

    expect(wrapper.text()).toContain('Newer')
    expect(wrapper.text()).not.toContain('Stale')
  })

  it('ignores a stale initial-load response that resolves after a concurrent silent reload already landed', async () => {
    let resolveInitialLoad: ((r: Response) => void) | undefined
    let callCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch(() => {
        callCount++
        if (callCount === 1) {
          return new Promise((resolve) => {
            resolveInitialLoad = resolve
          })
        }
        return Promise.resolve(jsonResponse([makeModule({ name: 'FromRefreshTick' })]))
      }),
    )
    const wrapper = mountPage()
    // The initial onMounted load() is still pending -- a refresh tick fires
    // a concurrent silentReload() that resolves first.
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.text()).toContain('FromRefreshTick')

    resolveInitialLoad?.(jsonResponse([makeModule({ name: 'StaleInitialLoad' })]))
    await flushPromises()

    expect(wrapper.text()).toContain('FromRefreshTick')
    expect(wrapper.text()).not.toContain('StaleInitialLoad')
  })

  it('suppresses the error state for a stale initial-load rejection once a newer silent reload already succeeded', async () => {
    let rejectInitialLoad: ((e: Error) => void) | undefined
    let callCount = 0
    vi.stubGlobal(
      'fetch',
      routedFetch(() => {
        callCount++
        if (callCount === 1) {
          return new Promise((_resolve, reject) => {
            rejectInitialLoad = reject
          })
        }
        return Promise.resolve(jsonResponse([makeModule({ name: 'FromRefreshTick' })]))
      }),
    )
    const wrapper = mountPage()
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.text()).toContain('FromRefreshTick')

    rejectInitialLoad?.(new Error('down'))
    await flushPromises()

    expect(wrapper.text()).toContain('FromRefreshTick')
    expect(wrapper.text()).not.toContain('Failed to load sync modules')
  })

  it('uses singular "camera" in the subtitle when exactly one camera exists', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch(() => Promise.resolve(jsonResponse([makeModule({ cameras: [makeModule().cameras[0]!] })]))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('1 of 1 camera armed')
    expect(wrapper.text()).not.toContain('1 of 1 cameras armed')
  })
})
