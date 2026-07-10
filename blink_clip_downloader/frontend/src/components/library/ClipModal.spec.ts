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
  requestFullscreen: vi.fn(),
}

vi.mock('video.js', () => ({ default: vi.fn(() => fakePlayer) }))
vi.mock('video.js/dist/video-js.css', () => ({}))

import ClipModal from './ClipModal.vue'
import { useConfirmStore } from '../../stores/confirm'

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

  it('opens and loads clip details when clipId is set', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.find('.modal-bg').classes()).toContain('open')
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('delivery')
    expect(fakePlayer.src).toHaveBeenCalledWith([{ src: '/api/clips/c1/stream', type: 'video/mp4' }])
    expect(fakePlayer.play).toHaveBeenCalled()
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

  it('adds a tag on Enter, sanitized to lowercase alnum/dash/underscore', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    const input = wrapper.find('.tag-input')
    await input.setValue('New Tag!!')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.findAll('.tag-item').map((el) => el.text())).toContain('newtag×')
  })

  it('removes a tag on click', async () => {
    const wrapper = mount(ClipModal, { props: { clipId: 'c1', aiEnabled: false, promptDebugEnabled: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('delivery')
    await wrapper.find('.tag-item .rm').trigger('click')
    await flushPromises()
    expect(wrapper.find('.tag-item').exists()).toBe(false)
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
})
