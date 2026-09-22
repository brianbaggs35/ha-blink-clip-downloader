import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import FileUpload from 'primevue/fileupload'
import PhotoFaceDetector from './PhotoFaceDetector.vue'
import { candidate, errorResponse, jsonResponse } from './testing'

vi.mock('./photo', () => ({ photoDataUrl: vi.fn(async (file: File) => `data:image/jpeg;base64,${file.name}`) }))

function mountDetector(available = true) {
  return mount(PhotoFaceDetector, { props: { available } })
}

async function choose(wrapper: ReturnType<typeof mountDetector>, ...names: string[]) {
  const files = names.map((name) => new File(['x'], name, { type: 'image/jpeg' }))
  await wrapper.findComponent(FileUpload).vm.$emit('select', { originalEvent: new Event('change'), files })
  await flushPromises()
}

describe('PhotoFaceDetector', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init: RequestInit) => {
        const image = JSON.parse(init.body as string).image_base64 as string
        if (image.endsWith('group.jpg'))
          return Promise.resolve(jsonResponse({ faces: [candidate('a'), candidate('b')] }))
        if (image.endsWith('empty.jpg')) return Promise.resolve(jsonResponse({ faces: [] }))
        if (image.endsWith('heic.jpg')) {
          return Promise.resolve(jsonResponse({ faces: [], error: "This photo couldn't be read" }))
        }
        if (image.endsWith('offline.jpg')) return Promise.reject(new TypeError('Failed to fetch'))
        if (image.endsWith('me.jpg')) return Promise.resolve(jsonResponse({ faces: [candidate('me')] }))
        return Promise.resolve(errorResponse(413, 'Maximum request body size exceeded'))
      }),
    )
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('offers every face in a photo and reports how many', async () => {
    const wrapper = mountDetector()
    await choose(wrapper, 'group.jpg')
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/detect', expect.objectContaining({ method: 'POST' }))
    expect(wrapper.emitted('found')).toEqual([[[candidate('a'), candidate('b')], 'group.jpg']])
    expect(wrapper.text()).toContain('2 faces found')
  })

  it('reads naturally for a single face', async () => {
    const wrapper = mountDetector()
    await choose(wrapper, 'me.jpg')
    expect(wrapper.text()).toContain('1 face found')
  })

  it('checks several photos, newest listed first', async () => {
    const wrapper = mountDetector()
    await choose(wrapper, 'group.jpg', 'empty.jpg')
    const rows = wrapper.findAll('.photo-results li')
    expect(rows.map((r) => r.find('.photo-name').text())).toEqual(['empty.jpg', 'group.jpg'])
    expect(rows[0].text()).toContain('No clear face found')
    expect(wrapper.emitted('found')).toHaveLength(1)
  })

  it('explains a photo the server could not read, or a failed request', async () => {
    const wrapper = mountDetector()
    await choose(wrapper, 'heic.jpg', 'broken.jpg', 'offline.jpg')
    const text = wrapper.text()
    expect(text).toContain("This photo couldn't be read")
    expect(text).toContain('Maximum request body size exceeded')
    expect(text).toContain('Detection failed — check your connection and try again')
    expect(wrapper.emitted('found')).toBeUndefined()
  })

  it('tolerates a select event with no files', async () => {
    const wrapper = mountDetector()
    await wrapper.findComponent(FileUpload).vm.$emit('select', { originalEvent: new Event('change') })
    await flushPromises()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('is disabled while face recognition is unavailable', () => {
    const wrapper = mountDetector(false)
    expect(wrapper.findComponent(FileUpload).props('disabled')).toBe(true)
  })
})
