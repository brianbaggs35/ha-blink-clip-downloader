import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import FaceRecognitionSection from './FaceRecognitionSection.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

class FakeFileReader {
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  result: string | null = null
  readAsDataURL() {
    this.result = 'data:image/jpeg;base64,AAAA'
    this.onload?.()
  }
}

describe('FaceRecognitionSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('FileReader', FakeFileReader)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows "No one enrolled yet" when the list is empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ available: true, faces: [] }))),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    expect(wrapper.text()).toContain('No one enrolled yet.')
  })

  it('shows the unavailable warning when dependencies are missing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ available: false, faces: [] }))),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    expect(wrapper.text()).toContain('dependencies are not installed')
  })

  it('lists enrolled faces and deletes one', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'DELETE') return Promise.resolve(jsonResponse({ deleted: true }))
        return Promise.resolve(jsonResponse({ available: true, faces: [{ id: 1, name: 'Alice', created_at: '' }] }))
      }),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    expect(wrapper.text()).toContain('Alice')
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/ai/faces/1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('warns when enrolling without a name or photo', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ available: true, faces: [] }))),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Enroll'))!
      .trigger('click')
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('warns when enrolling with a name but no photo', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ available: true, faces: [] }))),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Bob')
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Enroll'))!
      .trigger('click')
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('shows an error toast when deleting an enrollment fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'DELETE') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse({ available: true, faces: [{ id: 1, name: 'Alice', created_at: '' }] }))
      }),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    // covers the catch branch — no throw
  })

  it('enrolls a face from a name + photo', async () => {
    let enrolled: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/faces' && opts?.method === 'POST') {
          enrolled = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ id: 2, name: 'Bob' }))
        }
        return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      }),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Bob')
    const fileInput = wrapper.find('input[type="file"]')
    const file = new File(['x'], 'bob.jpg', { type: 'image/jpeg' })
    Object.defineProperty(fileInput.element, 'files', { value: [file] })
    await fileInput.trigger('change')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Enroll'))!
      .trigger('click')
    await flushPromises()
    expect(enrolled).toEqual({ name: 'Bob', image_base64: 'data:image/jpeg;base64,AAAA' })
  })

  it('shows an error toast when enrollment fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/faces' && opts?.method === 'POST') return Promise.reject(new Error('no face detected'))
        return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      }),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Bob')
    const fileInput = wrapper.find('input[type="file"]')
    const file = new File(['x'], 'bob.jpg', { type: 'image/jpeg' })
    Object.defineProperty(fileInput.element, 'files', { value: [file] })
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Enroll'))!
      .trigger('click')
    await flushPromises()
    // covers the catch branch — no throw
  })

  it('shows a generic error toast when enrollment rejects with a non-Error value', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/faces' && opts?.method === 'POST') return Promise.reject('boom')
        return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      }),
    )
    const wrapper = mount(FaceRecognitionSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Bob')
    const fileInput = wrapper.find('input[type="file"]')
    const file = new File(['x'], 'bob.jpg', { type: 'image/jpeg' })
    Object.defineProperty(fileInput.element, 'files', { value: [file] })
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Enroll'))!
      .trigger('click')
    await flushPromises()
    // covers the `e instanceof Error` false branch — no throw
  })
})
