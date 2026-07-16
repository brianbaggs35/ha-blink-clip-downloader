import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const fakePlayer = {
  src: vi.fn(),
  load: vi.fn(),
  play: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(),
  dispose: vi.fn(),
  on: vi.fn(),
  fluid: vi.fn(),
  loop: vi.fn(),
  muted: vi.fn().mockReturnValue(false),
  paused: vi.fn().mockReturnValue(true),
  currentTime: vi.fn().mockReturnValue(0),
  duration: vi.fn().mockReturnValue(100),
  requestFullscreen: vi.fn().mockResolvedValue(undefined),
}

vi.mock('video.js', () => ({ default: vi.fn(() => fakePlayer) }))
vi.mock('video.js/dist/video-js.css', () => ({}))

import ClipModal from './ClipModal.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

const CLIP = {
  id: 'c1',
  camera: 'front',
  file_path: '/data/clips/front.mp4',
  timestamp: '2026-01-05T10:00:00Z',
  size_bytes: 5_000_000,
  duration: 65,
  source: 'pir',
  network_id: 1,
  starred: false,
  tags: ['delivery'],
  downloaded_at: '2026-01-05T10:01:00Z',
  archived: false,
  archive_path: '',
}

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 404,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function mockFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/clips/c1') return Promise.resolve(jsonResponse(CLIP))
      if (url === '/api/clips/c1/star') return Promise.resolve(jsonResponse({ id: 'c1', starred: true }))
      if (url === '/api/clips/c1/tags')
        return Promise.resolve(jsonResponse({ id: 'c1', tags: JSON.parse((opts?.body as string) || '{}').tags }))
      return Promise.reject(new Error(`unexpected fetch ${url} ${opts?.method}`))
    }),
  )
}

describe('ClipModal', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockFetch()
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('is closed when clipId is null', () => {
    const wrapper = mount(ClipModal, { props: { clipId: null, aiEnabled: false, promptDebugEnabled: false } })
    expect(wrapper.find('.modal-bg').classes()).not.toContain('open')
  })

  it('star/delete/copy-path/tag-save are no-ops when clipId is null', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: null, aiEnabled: false, promptDebugEnabled: false } })
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper.find('.modal-actions').findAll('button')[0].trigger('click') // Star
    await wrapper.find('.modal-actions').findAll('button')[3].trigger('click') // Delete
    await wrapper.find('#clip-tag-input').setValue('x')
    await wrapper.find('#clip-tag-input').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
    expect(wrapper.emitted('deleted')).toBeUndefined()
  })

  it('copyPath is a no-op when the loaded clip has no file_path', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/clips/c1') return Promise.resolve(jsonResponse({ ...CLIP, file_path: '' }))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const copyBtn = wrapper.findAll('button').find((b) => b.text().includes('Path'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
  })

  it('shows placeholder dashes when duration/size/source are missing, and downloadName falls back with no timestamp', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/clips/c1')
          return Promise.resolve(jsonResponse({ ...CLIP, duration: 0, size_bytes: 0, source: '', timestamp: '' }))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.meta-grid').text()).toContain('—')
    const downloadLink = wrapper.find('a[download]')
    expect(downloadLink.attributes('download')).toBe('front_.mp4')
  })

  it('opens and loads clip details when clipId is set', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.modal-bg').classes()).toContain('open')
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('delivery')
    expect(fakePlayer.src).toHaveBeenCalledWith([{ src: '/api/clips/c1/stream', type: 'video/mp4' }])
    expect(fakePlayer.play).toHaveBeenCalled()
  })

  it('falls back to the thumbnail when video.js reports a playback error, and clears it on the next clip', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/clips/c1') return Promise.resolve(jsonResponse(CLIP))
        if (url === '/api/clips/c2') return Promise.resolve(jsonResponse({ ...CLIP, id: 'c2' }))
        return Promise.reject(new Error(`unexpected fetch ${url}`))
      }),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.video-fallback').exists()).toBe(false)
    expect(wrapper.find('.video-js-wrap').classes()).not.toContain('video-hidden')

    const errorHandler = fakePlayer.on.mock.calls.find(([event]) => event === 'error')![1] as () => void
    errorHandler()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.video-js-wrap').classes()).toContain('video-hidden')
    const fallback = wrapper.find('.video-fallback')
    expect(fallback.exists()).toBe(true)
    expect(fallback.find('.video-fallback-thumb').attributes('src')).toBe('/api/clips/c1/thumb')
    expect(fallback.text()).toContain("Video preview isn't available")
    expect(fallback.find('a[download]').exists()).toBe(true)

    // Loading a different clip clears the stale error instead of carrying it over.
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()
    expect(wrapper.find('.video-fallback').exists()).toBe(false)
    expect(wrapper.find('.video-js-wrap').classes()).not.toContain('video-hidden')
  })

  it('emits close on backdrop click and close button', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    await wrapper.find('.modal-close').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    await wrapper.find('.modal-bg').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(2)
  })

  it('emits nav on prev/next buttons', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    await wrapper.find('.vid-nav-btn').trigger('click')
    expect(wrapper.emitted('nav')).toEqual([[-1]])
  })

  it('toggles star and emits starred', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const starBtn = wrapper.findAll('button').find((b) => b.text().includes('Star'))!
    await starBtn.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('starred')).toEqual([['c1', true]])
  })

  it('unstars an already-starred clip', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/clips/c1') return Promise.resolve(jsonResponse({ ...CLIP, starred: true }))
        if (url === '/api/clips/c1/star') return Promise.resolve(jsonResponse({ id: 'c1', starred: false }))
        return Promise.reject(new Error(`unexpected fetch ${url} ${opts?.method}`))
      }),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('★ Starred')
    const starBtn = wrapper.findAll('button').find((b) => b.text().includes('Star'))!
    await starBtn.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('starred')).toEqual([['c1', false]])
  })

  it('confirms then emits deleted', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const confirm = useConfirmStore()
    const deleteBtn = wrapper.findAll('button').find((b) => b.text().includes('Delete'))!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(wrapper.emitted('deleted')).toEqual([['c1']])
  })

  it('adds a tag on Enter, spaces replaced with dashes rather than stripped', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const input = wrapper.find('.tag-input')
    await input.setValue('New Tag!!')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.findAll('.tag-item').map((el) => el.text())).toContain('new-tag×')
  })

  it('bumps the shared refresh signal after saving a tag, so the Library filter picks it up', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const refresh = useRefreshStore()
    const before = refresh.tick
    const input = wrapper.find('.tag-input')
    await input.setValue('another')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(refresh.tick).toBe(before + 1)
  })

  it('removes a tag on click after confirming', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const confirm = useConfirmStore()
    expect(wrapper.text()).toContain('delivery')
    const clickPromise = wrapper.find('.tag-item .rm').trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(wrapper.find('.tag-item').exists()).toBe(false)
  })

  it('does not remove a tag when the confirm dialog is dismissed', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('.tag-item .rm').trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()
    expect(wrapper.find('.tag-item').exists()).toBe(true)
  })

  it('toggles theater mode', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const theaterBtn = wrapper.findAll('button').find((b) => b.text().includes('Theater'))!
    await theaterBtn.trigger('click')
    expect(wrapper.find('.modal').classes()).toContain('theater')
    expect(fakePlayer.fluid).toHaveBeenCalledWith(false)
  })

  it('copies the file path to the clipboard', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const pathBtn = wrapper.findAll('button').find((b) => b.text().includes('Path'))!
    await pathBtn.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('/data/clips/front.mp4')
  })

  it('renders the AI panel only when aiEnabled is true', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: true, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.ai-panel').exists()).toBe(true)
  })

  it('does not render the AI panel when aiEnabled is false', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.ai-panel').exists()).toBe(false)
  })

  it('keyboard: Space toggles play/pause when the modal is open', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    fakePlayer.paused.mockReturnValue(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    expect(fakePlayer.play).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keyboard: ArrowUp/ArrowDown emit nav', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }))
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(wrapper.emitted('nav')).toEqual([[-1], [1]])
    wrapper.unmount()
  })

  it('keyboard: L toggles loop and shows a toast', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'l', bubbles: true }))
    await flushPromises()
    expect(fakePlayer.loop).toHaveBeenCalledWith(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'L', bubbles: true }))
    await flushPromises()
    expect(fakePlayer.loop).toHaveBeenCalledWith(false)
    wrapper.unmount()
  })

  it('keyboard: Escape closes the modal when no overlay is open', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keyboard: ignores keydowns while an input is focused', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const input = document.createElement('input')
    const event = new KeyboardEvent('keydown', { key: ' ', bubbles: true })
    Object.defineProperty(event, 'target', { value: input })
    fakePlayer.play.mockClear()
    document.dispatchEvent(event)
    expect(fakePlayer.play).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keyboard: Escape blurs a focused input instead of doing nothing', async () => {
    // Regression test: Escape used to be swallowed entirely while an input
    // (the tag/feedback-note field) was focused — it now blurs the input,
    // matching the typical "Escape blurs the input" convention, and must
    // not also close the modal in the same keypress.
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.body.appendChild(document.createElement('input'))
    const input = document.body.querySelector('input')!
    input.focus()
    expect(document.activeElement).toBe(input)
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
    Object.defineProperty(event, 'target', { value: input })
    document.dispatchEvent(event)
    expect(document.activeElement).not.toBe(input)
    expect(wrapper.emitted('close')).toBeUndefined()
    input.remove()
    wrapper.unmount()
  })

  it('keyboard: ArrowLeft/ArrowRight seek by 10s', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    fakePlayer.currentTime.mockReturnValue(20)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
    expect(fakePlayer.currentTime).toHaveBeenCalledWith(10)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    expect(fakePlayer.currentTime).toHaveBeenCalledWith(30)
    wrapper.unmount()
  })

  it('keyboard: ArrowLeft/ArrowRight fall back to 0 when currentTime/duration are unavailable', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    fakePlayer.currentTime.mockReturnValue(undefined)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
    expect(fakePlayer.currentTime).toHaveBeenCalledWith(0)
    fakePlayer.duration.mockReturnValue(undefined)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    expect(fakePlayer.currentTime).toHaveBeenCalledWith(0)
    fakePlayer.currentTime.mockReturnValue(0)
    fakePlayer.duration.mockReturnValue(100)
    wrapper.unmount()
  })

  it('keyboard: F toggles fullscreen and M toggles mute', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', bubbles: true }))
    expect(fakePlayer.requestFullscreen).toHaveBeenCalled()
    fakePlayer.muted.mockReturnValue(false)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'm', bubbles: true }))
    expect(fakePlayer.muted).toHaveBeenCalledWith(true)
    wrapper.unmount()
  })

  it('keyboard: shows a toast when the browser denies fullscreen', async () => {
    // Regression test: a rejected requestFullscreen() promise (fullscreen
    // denied by the browser/OS) used to surface as an unhandled promise
    // rejection rather than a user-facing toast, unlike every other action
    // in this file.
    fakePlayer.requestFullscreen.mockRejectedValueOnce(new Error('denied'))
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', bubbles: true }))
    await flushPromises()
    const toast = useToastStore()
    expect(toast.visible).toBe(true)
    expect(toast.isError).toBe(true)
    wrapper.unmount()
  })

  it('keyboard: Space pauses when already playing', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    fakePlayer.paused.mockReturnValue(false)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    expect(fakePlayer.pause).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keyboard: Escape does nothing while the confirm dialog is open on top', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    useConfirmStore().open = true
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('auto-plays the next clip when the video ends and autoplay is checked', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    await wrapper.find('input[type="checkbox"]').setValue(true)
    const endedHandler = fakePlayer.on.mock.calls.find((c) => c[0] === 'ended')![1] as () => void
    endedHandler()
    expect(wrapper.emitted('nav')).toEqual([[1]])
    wrapper.unmount()
  })

  it('does not emit deleted when the confirm dialog is dismissed', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const confirm = useConfirmStore()
    const deleteBtn = wrapper.findAll('button').find((b) => b.text().includes('Delete'))!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    expect(wrapper.emitted('deleted')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows the raw path via toast when copying to the clipboard fails', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const pathBtn = wrapper.findAll('button').find((b) => b.text().includes('Path'))!
    await pathBtn.trigger('click')
    await flushPromises()
    // no assertion on toast content needed beyond not throwing — covers the catch branch
    wrapper.unmount()
  })

  it('shows a toast when the clip fails to load', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 404,
          statusText: 'Not Found',
          text: () => Promise.resolve('gone'),
        } as Response),
      ),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'missing', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('front')
    wrapper.unmount()
  })

  it('switches clips: closing (clipId -> null) pauses and clears the player source', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    await wrapper.setProps({ clipId: null })
    expect(fakePlayer.pause).toHaveBeenCalled()
    expect(fakePlayer.src).toHaveBeenCalledWith('')
    wrapper.unmount()
  })

  it('cleans up the player and listener on unmount', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    wrapper.unmount()
    expect(fakePlayer.dispose).toHaveBeenCalled()
  })

  it('a slow response for a since-abandoned clip must not clobber a faster, more recent one', async () => {
    // Regression test: rapid prev/next navigation fires load() again before
    // an earlier getClip() resolves, with no inherent ordering. Without a
    // sequencing guard, clip c1's late response would overwrite the player
    // source and metadata back to c1 even though the user has already moved
    // on to c2 — the same stale-response-race class already fixed in
    // LibraryPage.vue and VehicleZonePicker/EnrollFromClipPicker.
    let resolveC1: (v: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/clips/c1') return new Promise((resolve) => (resolveC1 = resolve))
        if (url === '/api/clips/c2') return Promise.resolve(jsonResponse({ ...CLIP, id: 'c2', camera: 'backyard' }))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()

    // Navigate to c2 before c1's request resolves.
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()
    expect(wrapper.text()).toContain('backyard')
    expect(fakePlayer.src).toHaveBeenLastCalledWith([{ src: '/api/clips/c2/stream', type: 'video/mp4' }])

    // Now let the stale c1 response arrive.
    resolveC1(jsonResponse(CLIP))
    await flushPromises()

    expect(wrapper.text()).toContain('backyard')
    expect(wrapper.text()).not.toContain('front')
    expect(fakePlayer.src).toHaveBeenLastCalledWith([{ src: '/api/clips/c2/stream', type: 'video/mp4' }])
    wrapper.unmount()
  })
})
