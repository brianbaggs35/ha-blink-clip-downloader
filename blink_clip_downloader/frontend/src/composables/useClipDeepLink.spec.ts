import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { clipIdFromRoute, useClipDeepLink } from './useClipDeepLink'
import { useClipViewerStore } from '../stores/clipViewer'

const Host = defineComponent({
  setup() {
    useClipDeepLink()
    return {}
  },
  template: '<div />',
})

describe('clipIdFromRoute', () => {
  it('reads the clip from an alert link under the /app panel', () => {
    expect(clipIdFromRoute({ prefix: '/app/abc_blink_clip_downloader', path: '/clip/123' })).toBe('123')
  })

  it('reads the clip when Home Assistant splits the route after "clip"', () => {
    expect(clipIdFromRoute({ prefix: '/abc_blink_clip_downloader/clip', path: '/local_7' })).toBe('local_7')
  })

  it('decodes an escaped id', () => {
    expect(clipIdFromRoute({ prefix: '/app/x', path: '/clip/a%2Fb%20c' })).toBe('a/b c')
  })

  it('does not mistake a slug containing "clip" for a clip link', () => {
    expect(clipIdFromRoute({ prefix: '/app/abc_blink_clip_downloader', path: '' })).toBeNull()
  })

  it('returns null for no route, non-string parts, or an undecodable id', () => {
    expect(clipIdFromRoute(undefined)).toBeNull()
    expect(clipIdFromRoute({ prefix: 5, path: null })).toBeNull()
    expect(clipIdFromRoute({ prefix: '/app/x', path: '/clip/%E0%A4%A' })).toBeNull()
  })
})

describe('useClipDeepLink', () => {
  const ownParent = Object.getOwnPropertyDescriptor(window, 'parent') as PropertyDescriptor
  let frame: HTMLIFrameElement | undefined
  let parent: Window

  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
    vi.restoreAllMocks()
    frame?.remove()
    frame = undefined
    Object.defineProperty(window, 'parent', ownParent)
  })

  /** Stand in a real window for Home Assistant, so messages can come from it. */
  function embedIn() {
    const host = document.createElement('iframe')
    document.body.appendChild(host)
    frame = host
    parent = host.contentWindow as Window
    Object.defineProperty(window, 'parent', { value: parent, configurable: true })
    return vi.spyOn(parent, 'postMessage').mockImplementation(() => {})
  }

  function fromHomeAssistant(data: unknown, overrides: Partial<MessageEventInit> = {}) {
    window.dispatchEvent(
      new MessageEvent('message', {
        data,
        origin: window.location.origin,
        source: parent,
        ...overrides,
      }),
    )
  }

  function properties(path: string) {
    return { type: 'home-assistant/properties', route: { prefix: '/app/slug', path } }
  }

  it('opens the clip named by ?clip= and does not talk to a parent it lacks', () => {
    window.history.replaceState({}, '', '/?clip=c42')
    const post = vi.spyOn(window, 'postMessage')
    const wrapper = mount(Host)
    const viewer = useClipViewerStore()

    expect(viewer.clipId).toBe('c42')
    expect(viewer.seq).toBe(1)
    wrapper.unmount()
    expect(post).not.toHaveBeenCalled()
  })

  it('opens nothing without a clip in the URL', () => {
    mount(Host).unmount()
    expect(useClipViewerStore().seq).toBe(0)
  })

  it('subscribes to Home Assistant when embedded, and unsubscribes on unmount', () => {
    const post = embedIn()
    const wrapper = mount(Host)
    expect(post).toHaveBeenCalledWith({ type: 'home-assistant/subscribe-properties' }, window.location.origin)

    wrapper.unmount()
    expect(post).toHaveBeenLastCalledWith({ type: 'home-assistant/unsubscribe-properties' }, window.location.origin)
    fromHomeAssistant(properties('/clip/late'))
    expect(useClipViewerStore().seq).toBe(0)
  })

  it('opens the clip the panel route names, once per route', () => {
    embedIn()
    mount(Host)
    const viewer = useClipViewerStore()

    fromHomeAssistant(properties(''))
    expect(viewer.seq).toBe(0)

    fromHomeAssistant(properties('/clip/c1'))
    expect(viewer.clipId).toBe('c1')
    expect(viewer.seq).toBe(1)

    // A resend of the same route (a rotated phone) must not reopen it.
    fromHomeAssistant(properties('/clip/c1'))
    expect(viewer.seq).toBe(1)

    // A second alert tapped while the panel is open.
    fromHomeAssistant(properties('/clip/c2'))
    expect(viewer.clipId).toBe('c2')
    expect(viewer.seq).toBe(2)

    // Leaving the clip route and coming back to it counts as new.
    fromHomeAssistant(properties(''))
    fromHomeAssistant(properties('/clip/c2'))
    expect(viewer.seq).toBe(3)
  })

  it('ignores messages from another window, another origin, or of another type', () => {
    embedIn()
    mount(Host)
    const viewer = useClipViewerStore()

    fromHomeAssistant(properties('/clip/x'), { source: window })
    fromHomeAssistant(properties('/clip/x'), { origin: 'https://evil.example' })
    fromHomeAssistant({ type: 'home-assistant/navigate', route: { path: '/clip/x' } })
    fromHomeAssistant(null)

    expect(viewer.seq).toBe(0)
  })
})
