import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SecurityFeedTile from './SecurityFeedTile.vue'
import { getSecurityFeedSnapshot } from '../../api/securityFeed'

vi.mock('../../api/securityFeed', () => ({ getSecurityFeedSnapshot: vi.fn() }))

const snapshot = vi.mocked(getSecurityFeedSnapshot)

interface Pending {
  resolve: (blob: Blob) => void
  reject: (err: Error) => void
  signal: AbortSignal | undefined
}

/** Holds each snapshot request open until the test settles it, in order. */
function holdRequests(): Pending[] {
  const pending: Pending[] = []
  snapshot.mockImplementation(
    (_camera, signal) =>
      new Promise<Blob>((resolve, reject) => {
        pending.push({ resolve, reject, signal })
      }),
  )
  return pending
}

function mountTile(tick = 1) {
  return mount(SecurityFeedTile, { props: { camera: 'Front Door', tick } })
}

describe('SecurityFeedTile', () => {
  let urls = 0

  beforeEach(() => {
    vi.clearAllMocks()
    urls = 0
    // jsdom implements neither of these.
    URL.createObjectURL = vi.fn(() => `blob:snapshot-${++urls}`)
    URL.revokeObjectURL = vi.fn()
  })

  it('says it is loading, not that there is no snapshot, while the first one is on its way', async () => {
    holdRequests()
    const wrapper = mountTile()
    await flushPromises()

    expect(snapshot).toHaveBeenCalledWith('Front Door', expect.any(AbortSignal))
    expect(wrapper.find('img').classes()).toContain('secfeed-tile-image-hidden')
    expect(wrapper.find('img').attributes('src')).toBeUndefined()
    // A black box with no words in it is what a slow camera used to look like.
    expect(wrapper.find('.secfeed-tile-placeholder').text()).toBe('Loading snapshot…')
    expect(wrapper.text()).not.toContain('No snapshot available yet')
    expect(wrapper.text()).toContain('Front Door')
  })

  it('shows the snapshot once it arrives whole', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    const blob = new Blob(['jpeg'])
    pending[0].resolve(blob)
    await flushPromises()

    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    const img = wrapper.find('img')
    expect(img.attributes('src')).toBe('blob:snapshot-1')
    expect(img.attributes('alt')).toBe('Front Door snapshot')
    expect(img.classes()).not.toContain('secfeed-tile-image-hidden')
    expect(wrapper.find('.secfeed-tile-placeholder').exists()).toBe(false)
    expect(wrapper.text()).not.toContain("Couldn't refresh")
  })

  it('says there is no snapshot yet when the first request fails, and recovers on the next tick', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    pending[0].reject(new Error('Snapshot unavailable: 404'))
    await flushPromises()

    expect(wrapper.find('img').classes()).toContain('secfeed-tile-image-hidden')
    expect(wrapper.find('.secfeed-tile-placeholder').text()).toBe('No snapshot available yet')

    // Asking again does not turn the answer back into "loading": it is still
    // true that there is no snapshot, until one arrives.
    await wrapper.setProps({ tick: 2 })
    expect(wrapper.find('.secfeed-tile-placeholder').text()).toBe('No snapshot available yet')
    pending[1].resolve(new Blob(['jpeg']))
    await flushPromises()

    expect(wrapper.find('.secfeed-tile-placeholder').exists()).toBe(false)
    expect(wrapper.find('img').attributes('src')).toBe('blob:snapshot-1')
  })

  it('never says it is loading over a picture that is already showing', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    pending[0].resolve(new Blob(['jpeg']))
    await flushPromises()

    await wrapper.setProps({ tick: 2 })

    expect(snapshot).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.secfeed-tile-placeholder').exists()).toBe(false)
    expect(wrapper.find('img').attributes('src')).toBe('blob:snapshot-1')
  })

  it('keeps the last picture when a refresh fails instead of going black', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    pending[0].resolve(new Blob(['jpeg']))
    await flushPromises()

    await wrapper.setProps({ tick: 2 })
    pending[1].reject(new Error('Snapshot unavailable: 500'))
    await flushPromises()

    const img = wrapper.find('img')
    expect(img.attributes('src')).toBe('blob:snapshot-1')
    expect(img.classes()).not.toContain('secfeed-tile-image-hidden')
    expect(wrapper.find('.secfeed-tile-placeholder').exists()).toBe(false)
    // Said quietly, since a stale picture is otherwise indistinguishable
    // from a camera that simply has had no motion.
    expect(wrapper.find('output').text()).toBe("Couldn't refresh")
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
  })

  it('swaps in a newer snapshot, releases the old one and clears the stale note', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    pending[0].resolve(new Blob(['one']))
    await flushPromises()
    await wrapper.setProps({ tick: 2 })
    pending[1].reject(new Error('boom'))
    await flushPromises()
    expect(wrapper.find('output').exists()).toBe(true)

    await wrapper.setProps({ tick: 3 })
    pending[2].resolve(new Blob(['two']))
    await flushPromises()

    expect(wrapper.find('img').attributes('src')).toBe('blob:snapshot-2')
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:snapshot-1')
    expect(wrapper.find('output').exists()).toBe(false)
  })

  it('does not start a second request while one is still in flight', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    await wrapper.setProps({ tick: 2 })
    await wrapper.setProps({ tick: 3 })

    expect(snapshot).toHaveBeenCalledTimes(1)

    // Once it settles, the next tick asks again.
    pending[0].resolve(new Blob(['jpeg']))
    await flushPromises()
    await wrapper.setProps({ tick: 4 })
    expect(snapshot).toHaveBeenCalledTimes(2)
  })

  it('cancels a pending request and releases its picture when it goes away', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    await flushPromises()
    wrapper.unmount()

    expect(pending[0].signal?.aborted).toBe(true)
    // Nothing had arrived, so there was no object URL to release.
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    const shown = holdRequests()
    const second = mountTile()
    shown[0].resolve(new Blob(['jpeg']))
    await flushPromises()
    second.unmount()

    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:snapshot-1')
  })

  it('does not turn a snapshot that lands after it went away into an object URL', async () => {
    const pending = holdRequests()
    const wrapper = mountTile()
    wrapper.unmount()

    // A request that finished in the instant before the abort took effect.
    pending[0].resolve(new Blob(['jpeg']))
    await flushPromises()

    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
})
