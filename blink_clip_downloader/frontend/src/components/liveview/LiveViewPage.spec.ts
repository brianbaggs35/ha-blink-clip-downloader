import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import SelectButton from 'primevue/selectbutton'

let errorHandler: (() => void) | undefined
let fakePlayerErrorValue: { message: string } | null = null

const fakePlayerError = vi.fn(() => fakePlayerErrorValue)

const fakePlayer = {
  src: vi.fn(),
  play: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(),
  dispose: vi.fn(),
  error: fakePlayerError,
  // Default: already ready, so existing tests that don't care about ready()
  // timing see the same behavior as if there were no ready() wrap at all.
  // Tests that specifically exercise the wrap override this per-test.
  ready: vi.fn((cb: () => void) => cb()),
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
    fakePlayerErrorValue = null
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

  it('unmounting while the initial adoption check is in flight does not start timers on the dead instance', async () => {
    vi.useFakeTimers()
    let resolveStatus!: (v: LiveViewStatus) => void
    const deferred = new Promise<LiveViewStatus>((resolve) => {
      resolveStatus = resolve
    })
    const fetchMock = vi.fn((url: string) => {
      if (url === '/api/liveview/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
      if (url === '/api/liveview/status') return deferred.then((v) => jsonResponse(v))
      return Promise.resolve(jsonResponse({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    wrapper.unmount()
    // This session pre-existed us (adoption, not creation) — resolving
    // after teardown must not start a heartbeat for a session we don't
    // own, but also must not call stop, since we didn't create it either.
    resolveStatus({ active: true, session_id: 's1', camera: 'Front Door', state: 'live' })
    await flushPromises()

    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')).toBeFalsy()

    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/heartbeat')).toBeFalsy()
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
    errorHandler?.()

    const toast = useToastStore()
    expect(toast.isError).toBe(true)
    expect(toast.message).toBe('Live view playback error')
  })

  it('shows a useful fallback when Video.js does not provide an error message', async () => {
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    fakePlayerErrorValue = null
    errorHandler?.()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Live view playback failed. The stream could not be decoded by this browser.')
  })

  it('sources each live session once and shows the player error without retrying the HLS URL', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    expect(fakePlayer.src).toHaveBeenCalledTimes(1)
    fakePlayerErrorValue = { message: 'The media could not be loaded' }
    errorHandler?.()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Live view playback failed: The media could not be loaded')

    // A playback error must not clear sourcedSessionId. The HLS tech owns
    // manifest refreshes; reassigning the same URL here starts another load
    // loop and can make a healthy Blink session look throttled.
    await vi.advanceTimersByTimeAsync(12000)
    await flushPromises()
    expect(fakePlayer.src).toHaveBeenCalledTimes(1)
  })

  it('sourcing the player waits for player.ready() instead of calling src() immediately', async () => {
    // Video.js's tech (VHS, for HLS) isn't necessarily mounted the instant
    // videojs() returns — calling .src() before player.ready() fires used
    // to produce a spurious, immediately-superseded "media could not be
    // loaded" error even though the source itself was fine. Defer ready()'s
    // callback here (unlike the shared fakePlayer default, which invokes it
    // synchronously) to prove src()/play() genuinely wait for it rather
    // than firing eagerly.
    let readyCallback: (() => void) | undefined
    fakePlayer.ready.mockImplementationOnce((cb: () => void) => {
      readyCallback = cb
    })
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    mountPage()
    await flushPromises()

    expect(fakePlayer.src).not.toHaveBeenCalled()
    expect(fakePlayer.play).not.toHaveBeenCalled()
    expect(readyCallback).toBeTypeOf('function')

    readyCallback?.()

    expect(fakePlayer.src).toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s1/stream.m3u8', type: 'application/x-mpegURL' },
    ])
    expect(fakePlayer.play).toHaveBeenCalled()
  })

  it('does not source a queued player callback after Stop invalidates the session', async () => {
    let readyCallback: (() => void) | undefined
    fakePlayer.ready.mockImplementationOnce((cb: () => void) => {
      readyCallback = cb
    })
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    const stopBtn = wrapper.findAll('button').find((b) => b.text().includes('Stop'))
    expect(stopBtn).toBeTruthy()
    await stopBtn!.trigger('click')
    await flushPromises()

    readyCallback?.()
    errorHandler?.()
    expect(fakePlayer.src).not.toHaveBeenCalled()
    expect(useToastStore().isError).toBe(false)
  })

  it('does not source a queued callback after the backend reports a different session', async () => {
    vi.useFakeTimers()
    let staleReadyCallback: (() => void) | undefined
    fakePlayer.ready.mockImplementationOnce((cb: () => void) => {
      staleReadyCallback = cb
    })
    const routes: Routes = {
      cameras: ['Front Door', 'Backyard'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    routes.status = { active: true, session_id: 's2', camera: 'Backyard', state: 'live' }
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    staleReadyCallback?.()

    expect(fakePlayer.src).not.toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s1/stream.m3u8', type: 'application/x-mpegURL' },
    ])
    expect(fakePlayer.src).toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s2/stream.m3u8', type: 'application/x-mpegURL' },
    ])
    wrapper.unmount()
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

  it('keeps the video element hidden while starting, even though the session is already active', async () => {
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

    expect(wrapper.find('.video-js-wrap').classes()).toContain('video-hidden')
    expect(fakePlayer.src).not.toHaveBeenCalled()
  })

  it('unmounting while a start request is still in flight stops the session it just created, without leaking a heartbeat timer', async () => {
    vi.useFakeTimers()
    let resolveStart!: (v: LiveViewStatus) => void
    const deferred = new Promise<LiveViewStatus>((resolve) => {
      resolveStart = resolve
    })
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/liveview/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
      if (url === '/api/liveview/status') return Promise.resolve(jsonResponse(INACTIVE))
      if (url === '/api/liveview/start' && opts?.method === 'POST') return deferred.then((v) => jsonResponse(v))
      if (url === '/api/liveview/stop' && opts?.method === 'POST')
        return Promise.resolve(jsonResponse({ stopped: true }))
      return Promise.resolve(jsonResponse({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Front Door')
    await wrapper.vm.$nextTick()

    wrapper.unmount()
    // Nothing to stop yet from onUnmounted's own point of view — the
    // session this component is about to create doesn't exist until the
    // deferred /start response below resolves.
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')).toBeFalsy()

    resolveStart({ active: true, session_id: 's1', camera: 'Front Door', state: 'starting' })
    await flushPromises()

    const stopCall = fetchMock.mock.calls.find(([url]) => url === '/api/liveview/stop')
    expect(stopCall).toBeTruthy()
    expect(JSON.parse((stopCall![1] as RequestInit).body as string)).toEqual({ session_id: 's1' })

    // Prove there's no orphaned heartbeat timer keeping the session (that
    // we just told the backend to stop) alive in the background.
    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/heartbeat')).toBeFalsy()
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

  it('hides the old camera stream while switching cameras, until the new one is live', async () => {
    let resolveStart!: (v: LiveViewStatus) => void
    const deferred = new Promise<LiveViewStatus>((resolve) => {
      resolveStart = resolve
    })
    const routes: Routes = {
      cameras: ['Front Door', 'Backyard'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/liveview/start' && opts?.method === 'POST') return deferred.then((v) => jsonResponse(v))
        return routedFetch(routes)(url, opts)
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.video-js-wrap').classes()).not.toContain('video-hidden')

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Backyard')
    await wrapper.vm.$nextTick()

    // Mid-switch: `status` still reports the old camera as active/live
    // (it hasn't changed yet), but the video must stay hidden rather than
    // keep showing Front Door's stream next to the "starting" placeholder.
    expect(wrapper.find('.video-js-wrap').classes()).toContain('video-hidden')

    resolveStart({ active: true, session_id: 's2', camera: 'Backyard', state: 'live' })
    await flushPromises()
    expect(wrapper.find('.video-js-wrap').classes()).not.toContain('video-hidden')
  })

  it('pressing stop while switching cameras resets immediately and stops the new session once it arrives', async () => {
    let resolveStart!: (v: LiveViewStatus) => void
    const deferred = new Promise<LiveViewStatus>((resolve) => {
      resolveStart = resolve
    })
    const routes: Routes = {
      cameras: ['Front Door', 'Backyard'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/liveview/start' && opts?.method === 'POST') return deferred.then((v) => jsonResponse(v))
      return routedFetch(routes)(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'Backyard')
    await wrapper.vm.$nextTick()

    const stopBtn = wrapper.findAll('button').find((b) => b.text().includes('Stop'))
    expect(stopBtn).toBeTruthy()
    await stopBtn!.trigger('click')
    await flushPromises()

    // Stop must take effect immediately -- not get stuck showing "Starting
    // live view..." just because a select() it superseded hasn't resolved
    // yet -- and must stop the *old* (Front Door) session right away.
    expect(wrapper.text()).toContain('Select a camera above to start watching')
    const stopCallsSoFar = fetchMock.mock.calls.filter(([url]) => url === '/api/liveview/stop')
    expect(stopCallsSoFar).toHaveLength(1)
    expect(JSON.parse((stopCallsSoFar[0][1] as RequestInit).body as string)).toEqual({ session_id: 's1' })

    resolveStart({ active: true, session_id: 's2', camera: 'Backyard', state: 'live' })
    await flushPromises()

    // The belated Backyard session must also get cleaned up rather than
    // resurrecting the player after Stop was already pressed.
    const stopCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/liveview/stop')
    expect(stopCalls).toHaveLength(2)
    expect(JSON.parse((stopCalls[1][1] as RequestInit).body as string)).toEqual({ session_id: 's2' })
    expect(wrapper.text()).toContain('Select a camera above to start watching')
    // s1 (Front Door) was legitimately sourced back on mount, when it was
    // still live — what must never happen is s2 (Backyard) getting sourced
    // after Stop was pressed.
    expect(fakePlayer.src).not.toHaveBeenCalledWith([
      { src: '/api/liveview/hls/s2/stream.m3u8', type: 'application/x-mpegURL' },
    ])
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

  it('navigating away and back before the stop request lands does not re-select the stopped camera', async () => {
    // Regression test: onUnmounted's stop call is fire-and-forget (Vue
    // can't block unmount on it), so quickly navigating away and back used
    // to race a fresh mount's own status check against that still-in-flight
    // stop request — the fresh mount could see the old session as still
    // active and re-select its camera moments before the delayed stop
    // finally landed. pendingStop (module-scoped, shared across this
    // component's own mount/unmount cycles) closes that race by making the
    // new mount wait for it first.
    let resolveStop!: (v: { stopped: boolean }) => void
    const stopDeferred = new Promise<{ stopped: boolean }>((resolve) => {
      resolveStop = resolve
    })
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    let statusCallCount = 0
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/liveview/status') statusCallCount++
      if (url === '/api/liveview/stop' && opts?.method === 'POST') {
        return stopDeferred.then((v) => jsonResponse(v))
      }
      return routedFetch(routes)(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper1 = mountPage()
    await flushPromises()
    expect(statusCallCount).toBe(1) // the initial adoption check

    wrapper1.unmount() // fires the (still-unresolved) stop request

    const wrapper2 = mountPage() // navigated back before it landed
    await flushPromises()

    // wrapper2's own status check must not have run yet — it's waiting on
    // the pending stop — so it can't have adopted (or shown selected) a
    // session that's a moment away from being torn down.
    expect(statusCallCount).toBe(1)
    expect(wrapper2.findComponent(SelectButton).props('modelValue')).toBeNull()

    // The stop now lands for real, and the session is genuinely gone.
    routes.status = { active: false }
    resolveStop({ stopped: true })
    await flushPromises()

    expect(statusCallCount).toBe(2) // wrapper2 proceeded once unblocked
    expect(wrapper2.findComponent(SelectButton).props('modelValue')).toBeNull()

    wrapper2.unmount()
  })

  it('unmounting again while still waiting on a pending stop from a previous mount does not adopt or start timers', async () => {
    vi.useFakeTimers()
    let resolveStop!: (v: { stopped: boolean }) => void
    const stopDeferred = new Promise<{ stopped: boolean }>((resolve) => {
      resolveStop = resolve
    })
    const routes: Routes = {
      cameras: ['Front Door'],
      status: { active: true, session_id: 's1', camera: 'Front Door', state: 'live' },
    }
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/liveview/stop' && opts?.method === 'POST') {
        return stopDeferred.then((v) => jsonResponse(v))
      }
      return routedFetch(routes)(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper1 = mountPage()
    await flushPromises()
    wrapper1.unmount() // pendingStop now set, unresolved

    const wrapper2 = mountPage()
    await flushPromises() // wrapper2's onMounted is now blocked awaiting pendingStop
    wrapper2.unmount() // ...torn down again before that ever resolves

    resolveStop({ stopped: true })
    await flushPromises()

    // Neither adoption nor a heartbeat timer must have started for the
    // now-doubly-dead wrapper2 instance.
    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()
    expect(fetchMock.mock.calls.find(([url]) => url === '/api/liveview/heartbeat')).toBeFalsy()
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
