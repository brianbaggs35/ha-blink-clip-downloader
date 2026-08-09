import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import SelectButton from 'primevue/selectbutton'

let errorHandler: (() => void) | undefined

const fakePlayer = {
  src: vi.fn(),
  play: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(),
  dispose: vi.fn(),
  on: vi.fn((event: string, cb: () => void) => {
    if (event === 'error') errorHandler = cb
  }),
}

vi.mock('video.js', () => ({ default: vi.fn(() => fakePlayer) }))
vi.mock('video.js/dist/video-js.css', () => ({}))

import LiveViewPage from './LiveViewPage.vue'
import { useToastStore } from '../../stores/toast'
import type { LiveViewStatus } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

const INACTIVE: LiveViewStatus = { active: false }

interface Routes {
  cameras?: string[]
  camerasFail?: boolean
  status?: LiveViewStatus
  startResult?: LiveViewStatus
  startFail?: boolean
}

function routedFetch(routes: Routes) {
  return vi.fn((url: string, opts?: RequestInit) => {
    const method = opts?.method
    if (url === '/api/liveview/cameras') {
      if (routes.camerasFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse({ cameras: routes.cameras ?? [] }))
    }
    if (url === '/api/liveview/status') {
      return Promise.resolve(jsonResponse(routes.status ?? INACTIVE))
    }
    if (url === '/api/liveview/start' && method === 'POST') {
      if (routes.startFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse(routes.startResult ?? INACTIVE))
    }
    if (url === '/api/liveview/stop' && method === 'POST') {
      return Promise.resolve(jsonResponse({ stopped: true }))
    }
    if (url === '/api/liveview/heartbeat' && method === 'POST') {
      return Promise.resolve(jsonResponse({ ok: true }))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function mountPage() {
  return mount(LiveViewPage, { global: { plugins: [PrimeVue] } })
}

describe('LiveViewPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.clearAllMocks()
    errorHandler = undefined
  })

  it('loads and renders the camera list', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: ['Front Door', 'Backyard'] }))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('Backyard')
  })

  it('shows an empty state with zero cameras', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: [] }))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('No cameras found')
  })

  it('shows a toast if loading the camera list fails, without crashing', async () => {
    vi.stubGlobal('fetch', routedFetch({ camerasFail: true }))
    const wrapper = mountPage()
    await flushPromises()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toContain('Failed to load cameras')
    expect(wrapper.text()).toContain('No cameras found')
  })

  it('adopts an already-active session on mount instead of showing an empty picker', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    expect(fakePlayer.src).toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s1/stream.m3u8', type: 'application/x-mpegURL' },
    ])
    expect(wrapper.findComponent(SelectButton).props('modelValue')).toBe('Front Door')
  })

  it('shows a toast when the Video.js player itself reports an error', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    mountPage()
    await flushPromises()

    expect(errorHandler).toBeTypeOf('function')
    errorHandler?.()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toBe('Live view playback error')
  })

  it('selecting a camera starts a session and shows a starting placeholder', async () => {
    let resolveStart!: (v: LiveViewStatus) => void
    const deferred = new Promise<LiveViewStatus>((resolve) => {
      resolveStart = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/liveview/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
        if (url === '/api/liveview/status') return Promise.resolve(jsonResponse(INACTIVE))
        if (url === '/api/liveview/start' && opts?.method === 'POST') return deferred.then((v) => jsonResponse(v))
        return Promise.resolve(jsonResponse({}))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Starting live view')

    resolveStart({ active: true, session_id: 's1', camera: 'Front Door', state: 'starting' })
    await flushPromises()
  })

  it('a status poll transitions starting to live and sets the player source', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: INACTIVE,
      startResult: { active: true, session_id: 's1', camera: 'Front Door', state: 'starting' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()
    expect(fakePlayer.src).not.toHaveBeenCalled()

    routes.status = { active: true, session_id: 's1', camera: 'Front Door', state: 'live' }
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()

    expect(fakePlayer.src).toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s1/stream.m3u8', type: 'application/x-mpegURL' },
    ])
  })

  it('surfaces an error message reported by the status poll', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: INACTIVE,
      startResult: { active: true, session_id: 's1', camera: 'Front Door', state: 'starting' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    routes.status = {
      active: true,
      session_id: 's1',
      camera: 'Front Door',
      state: 'error',
      error: 'ffmpeg exited unexpectedly',
    }
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()

    expect(wrapper.text()).toContain('ffmpeg exited unexpectedly')
  })

  it('shows a toast when starting a session fails', async () => {
    const routes: Routes = { cameras: ['Front Door'], status: INACTIVE, startFail: true }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
  })

  it('Stop calls stopLiveView with the current session id and resets to picker state', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    const stopBtn = wrapper.findAll('button').find((b) => b.text().includes('Stop'))
    expect(stopBtn).toBeTruthy()
    await stopBtn!.trigger('click')
    await flushPromises()

    const stopCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')
    expect(stopCall).toBeTruthy()
    expect(JSON.parse((stopCall![1] as RequestInit).body as string)).toEqual({ session_id: 's1' })
    expect(wrapper.text()).toContain('Select a camera above to start watching')
    expect(fakePlayer.pause).toHaveBeenCalled()
  })

  it('sends a heartbeat on its own interval while a session is active', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    mountPage()
    await flushPromises()

    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()

    const heartbeatCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/heartbeat')
    expect(heartbeatCall).toBeTruthy()
    expect(JSON.parse((heartbeatCall![1] as RequestInit).body as string)).toEqual({ session_id: 's1' })
  })

  it('switching cameras while live starts a new session and swaps the player source', async () => {
    const routes: Routes = {
      cameras: ['Front Door', 'Backyard'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    routes.startResult = { active: true, session_id: 's2', camera: 'Backyard', state: 'live' }
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()

    expect(fakePlayer.src).toHaveBeenLastCalledWith([
      { src: '/api/liveview/hls/s2/stream.m3u8', type: 'application/x-mpegURL' },
    ])
    expect(wrapper.findComponent(SelectButton).props('modelValue')).toBe('Backyard')
  })

  it('unmounting stops the active session and disposes the player', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    wrapper.unmount()
    await flushPromises()

    const stopCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')
    expect(stopCall).toBeTruthy()
    expect(fakePlayer.dispose).toHaveBeenCalled()
  })

  it('unmounting without an active session does not call stop', async () => {
    const routes: Routes = { cameras: ['Front Door'], status: INACTIVE }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    wrapper.unmount()
    await flushPromises()

    const stopCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')
    expect(stopCall).toBeFalsy()
    expect(fakePlayer.dispose).not.toHaveBeenCalled()
  })

  it('a status poll reporting the session ended resets to picker state', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    // Session ended on its own (idle timeout, hard cap, or another tab
    // switching cameras) -- the next status poll must notice and reset.
    routes.status = INACTIVE
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()

    expect(wrapper.text()).toContain('Select a camera above to start watching')
    expect(fakePlayer.pause).toHaveBeenCalled()
  })

  it('shows a toast if the stop request itself fails', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/liveview/stop') return Promise.resolve(jsonResponse({}, false))
        return routedFetch(routes)(url, opts)
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const stopBtn = wrapper.findAll('button').find((b) => b.text().includes('Stop'))
    await stopBtn!.trigger('click')
    await flushPromises()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toContain('Failed to stop live view')
  })

  it('falls back to a generic message when start rejects with a non-Error value', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/liveview/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
        if (url === '/api/liveview/status') return Promise.resolve(jsonResponse(INACTIVE))
        if (url === '/api/liveview/start') return Promise.reject('boom')
        return Promise.resolve(jsonResponse({}))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toBe('Failed to start live view')
  })

  it('falls back to a generic message when the error status has no error text', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: INACTIVE,
      startResult: { active: true, session_id: 's1', camera: 'Front Door', state: 'starting' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    routes.status = { active: true, session_id: 's1', camera: 'Front Door', state: 'error' }
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()

    expect(wrapper.text()).toContain('Live view stopped unexpectedly.')
  })

  it('sendHeartbeat is a no-op if the active session has no session id', async () => {
    vi.useFakeTimers()
    // Malformed/defensive case: active with no session_id shouldn't happen
    // from the real backend, but must not crash the heartbeat timer. status
    // must keep reporting active (matching startResult), or the 4s status
    // poll would see "inactive" first and stop the timers before the 15s
    // heartbeat interval ever gets a chance to fire.
    const activeNoId: LiveViewStatus = { active: true, camera: 'Front Door', state: 'starting' }
    const routes: Routes = {
      cameras: ['Front Door'],
      status: activeNoId,
      startResult: activeNoId,
    }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()

    const heartbeatCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/heartbeat')
    expect(heartbeatCall).toBeFalsy()
  })

  it('selecting no camera (SelectButton emitting null) is a no-op', async () => {
    const routes: Routes = { cameras: ['Front Door'], status: INACTIVE }
    const fetchMock = routedFetch(routes)
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', null)
    await flushPromises()

    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/start')).toBeFalsy()
  })
})
