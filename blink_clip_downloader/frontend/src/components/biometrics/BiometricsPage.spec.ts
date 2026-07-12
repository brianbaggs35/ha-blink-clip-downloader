import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import FileUpload from 'primevue/fileupload'
import BiometricsPage from './BiometricsPage.vue'
import { useConfirmStore } from '../../stores/confirm'
import type { FaceEnrollment } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function faceEnrollment(overrides: Partial<FaceEnrollment> = {}): FaceEnrollment {
  return { id: 1, name: 'Brian', created_at: '2026-01-01T00:00:00Z', approved: true, ...overrides }
}

function mountPage() {
  return mount(BiometricsPage, { global: { plugins: [PrimeVue] } })
}

function stubFileReader(result = 'data:image/jpeg;base64,AAAA') {
  class FakeFileReader {
    result: string | null = null
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    readAsDataURL() {
      this.result = result
      queueMicrotask(() => this.onload?.())
    }
  }
  vi.stubGlobal('FileReader', FakeFileReader)
}

// BiometricsPage always mounts EnrollFromClipPicker (the default enrollment
// mode), which fetches /api/cameras, /api/clips, and .../frames on its own
// as soon as it appears — every fetch mock in this file needs to answer
// those regardless of what the individual test actually cares about, or
// PrimeVue's Select crashes on a non-array `options`.
function routedFetch(extra: (url: string, init?: RequestInit) => Promise<Response> | undefined) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse([]))
    if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
    if (url.includes('/api/clips')) return Promise.resolve(jsonResponse([]))
    return extra(url, init) ?? Promise.reject(new Error(`unhandled fetch: ${url}`))
  })
}

function stubFaces(faces: FaceEnrollment[], available = true) {
  vi.stubGlobal(
    'fetch',
    routedFetch(() => Promise.resolve(jsonResponse({ available, faces }))),
  )
}

async function switchToPhotoMode(wrapper: ReturnType<typeof mountPage>) {
  const photoBtn = wrapper.findAll('button').find((b) => b.text().includes('Upload a photo'))!
  await photoBtn.trigger('click')
}

describe('BiometricsPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    stubFileReader()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an empty state when no one is enrolled', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('No one enrolled yet')
  })

  it('shows the dependency-missing warning when unavailable', async () => {
    stubFaces([], false)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('not installed')
  })

  it('shows the advanced-feature label and safety/privacy guarantee banners', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Advanced feature')
    expect(wrapper.text()).toContain('stays local')
    expect(wrapper.text()).toContain('never sent to any AI provider')
    expect(wrapper.text()).toContain('all-or-nothing')
    expect(wrapper.text()).toContain('everything works exactly as it does without it')
  })

  it('groups multiple enrolled photos of the same person into one card', async () => {
    stubFaces([
      faceEnrollment({ id: 1, name: 'Brian', approved: true, created_at: '2026-01-02T00:00:00Z' }),
      faceEnrollment({ id: 2, name: 'Brian', approved: true, created_at: '2026-01-01T00:00:00Z' }),
      faceEnrollment({ id: 3, name: 'Nanny', approved: false }),
    ])
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.person-card')).toHaveLength(2)
    expect(wrapper.text()).toContain('2 photos')
    expect(wrapper.text()).toContain('1 photo')
    expect(wrapper.text()).toContain('1 of 2 enrolled people are approved')
  })

  it('shows a "Partially approved" badge when a person\'s photos disagree on approval', async () => {
    stubFaces([
      faceEnrollment({ id: 1, name: 'Brian', approved: true }),
      faceEnrollment({ id: 2, name: 'Brian', approved: false }),
    ])
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Partially approved')
  })

  it('rejects enrollment without a name (photo mode)', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'POST')).toBe(
      false,
    )
  })

  it('rejects enrollment without a photo (photo mode)', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    await wrapper.find('#biometrics-name').setValue('Brian')
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'POST')).toBe(
      false,
    )
  })

  it('rejects enrolling from a clip with no frames selected', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('#biometrics-name').setValue('Brian')
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'POST')).toBe(
      false,
    )
  })

  it('enrolls a person from a photo, defaulting to approved', async () => {
    const calls: [string, RequestInit?][] = []
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        calls.push([url, init])
        if (init?.method === 'POST') return Promise.resolve(jsonResponse({ id: 1, name: 'Brian', approved: true }))
        return Promise.resolve(jsonResponse({ available: true, faces: calls.length > 1 ? [faceEnrollment()] : [] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    await wrapper.find('#biometrics-name').setValue('Brian')
    const fileUpload = wrapper.findComponent(FileUpload)
    const file = new File(['x'], 'brian.jpg', { type: 'image/jpeg' })
    await fileUpload.vm.$emit('select', { files: [file] })
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()

    const postCall = calls.find(([, init]) => init?.method === 'POST')!
    const body = JSON.parse(postCall[1]!.body as string)
    expect(body).toEqual({ name: 'Brian', image_base64: 'data:image/jpeg;base64,AAAA', approved: true })
  })

  it('enrolls a person as not-approved when the toggle is switched off', async () => {
    const calls: [string, RequestInit?][] = []
    vi.stubGlobal(
      'fetch',
      routedFetch((url, init) => {
        calls.push([url, init])
        if (init?.method === 'POST') return Promise.resolve(jsonResponse({ id: 1, name: 'Nanny', approved: false }))
        return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    await wrapper.find('#biometrics-name').setValue('Nanny')
    const fileUpload = wrapper.findComponent(FileUpload)
    await fileUpload.vm.$emit('select', { files: [new File(['x'], 'nanny.jpg', { type: 'image/jpeg' })] })
    await wrapper.find('#biometrics-approved-on-enroll').setValue(false)
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()

    const postCall = calls.find(([, init]) => init?.method === 'POST')!
    const body = JSON.parse(postCall[1]!.body as string)
    expect(body.approved).toBe(false)
  })

  it('shows an error toast-worthy message when photo enrollment fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'POST') return Promise.resolve(jsonResponse({ error: 'No face detected' }, false))
        return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    await wrapper.find('#biometrics-name').setValue('Brian')
    const fileUpload = wrapper.findComponent(FileUpload)
    await fileUpload.vm.$emit('select', { files: [new File(['x'], 'brian.jpg', { type: 'image/jpeg' })] })
    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    await enrollBtn.trigger('click')
    await flushPromises()
    expect(wrapper.exists()).toBe(true) // did not throw
  })

  it("toggles a person's approved status for every enrolled photo (bulk by-name)", async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'PATCH') return Promise.resolve(jsonResponse({ updated: true }))
        return Promise.resolve(
          jsonResponse({
            available: true,
            faces: [faceEnrollment({ id: 1, approved: true }), faceEnrollment({ id: 2, approved: true })],
          }),
        )
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const toggle = wrapper.find('#biometrics-approved-Brian')
    await toggle.setValue(false)
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      '/api/ai/faces/by-name/Brian',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ approved: false }) }),
    )
  })

  it('renames a person inline, applied to every enrolled photo (bulk by-name)', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'PATCH') return Promise.resolve(jsonResponse({ updated: true }))
        return Promise.resolve(jsonResponse({ available: true, faces: [faceEnrollment({ name: 'Brain' })] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const editBtn = wrapper.findAll('button').find((b) => b.text() === '✎')!
    await editBtn.trigger('click')
    const input = wrapper.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'Brain')!
    await input.setValue('Brian')
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      '/api/ai/faces/by-name/Brain',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: 'Brian' }) }),
    )
  })

  it('cancels an inline rename without saving', async () => {
    stubFaces([faceEnrollment({ name: 'Brian' })])
    const wrapper = mountPage()
    await flushPromises()
    const editBtn = wrapper.findAll('button').find((b) => b.text() === '✎')!
    await editBtn.trigger('click')
    const cancelBtn = wrapper.findAll('button').find((b) => b.text() === 'Cancel')!
    await cancelBtn.trigger('click')
    expect(wrapper.text()).toContain('Brian')
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'PATCH')).toBe(false)
  })

  it('rejects saving an empty name', async () => {
    stubFaces([faceEnrollment({ name: 'Brian' })])
    const wrapper = mountPage()
    await flushPromises()
    const editBtn = wrapper.findAll('button').find((b) => b.text() === '✎')!
    await editBtn.trigger('click')
    const input = wrapper.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'Brian')!
    await input.setValue('   ')
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'PATCH')).toBe(false)
  })

  it('removes every enrolled photo for a person after confirming (bulk by-name)', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'DELETE') return Promise.resolve(jsonResponse({ deleted: true }))
        return Promise.resolve(
          jsonResponse({
            available: true,
            faces: [faceEnrollment({ id: 1 }), faceEnrollment({ id: 2 })],
          }),
        )
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const confirm = useConfirmStore()
    const removeBtn = wrapper.findAll('button').find((b) => b.text().includes('Remove'))!
    const clickPromise = removeBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/by-name/Brian', { method: 'DELETE' })
  })

  it('does not remove a person when the confirmation is declined', async () => {
    stubFaces([faceEnrollment()])
    const wrapper = mountPage()
    await flushPromises()
    const confirm = useConfirmStore()
    const removeBtn = wrapper.findAll('button').find((b) => b.text().includes('Remove'))!
    const clickPromise = removeBtn.trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'DELETE')).toBe(false)
  })

  it('shows an error toast-worthy message when removal fails', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'DELETE') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse({ available: true, faces: [faceEnrollment()] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const confirm = useConfirmStore()
    const removeBtn = wrapper.findAll('button').find((b) => b.text().includes('Remove'))!
    const clickPromise = removeBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(wrapper.exists()).toBe(true) // did not throw
  })

  it('saves an inline rename by pressing Enter', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch((_url, init) => {
        if (init?.method === 'PATCH') return Promise.resolve(jsonResponse({ updated: true }))
        return Promise.resolve(jsonResponse({ available: true, faces: [faceEnrollment({ name: 'Brain' })] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const editBtn = wrapper.findAll('button').find((b) => b.text() === '✎')!
    await editBtn.trigger('click')
    const input = wrapper.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'Brain')!
    await input.setValue('Brian')
    await input.trigger('keyup.enter')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      '/api/ai/faces/by-name/Brain',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: 'Brian' }) }),
    )
  })

  it('switches back to "From a clip" mode after visiting photo mode', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    expect(wrapper.find('.enroll-from-clip').exists()).toBe(false)

    const clipBtn = wrapper.findAll('button').find((b) => b.text().includes('From a clip'))!
    await clipBtn.trigger('click')
    await flushPromises()
    expect(wrapper.find('.enroll-from-clip').exists()).toBe(true)
  })

  it('clears the selected photo in photo mode', async () => {
    stubFaces([])
    const wrapper = mountPage()
    await flushPromises()
    await switchToPhotoMode(wrapper)
    const fileUpload = wrapper.findComponent(FileUpload)
    await fileUpload.vm.$emit('select', { files: [new File(['x'], 'brian.jpg', { type: 'image/jpeg' })] })
    await flushPromises()
    expect(wrapper.find('img.preview-thumb').exists()).toBe(true)
    const clearBtn = wrapper.findAll('button').find((b) => b.text() === 'Clear')!
    await clearBtn.trigger('click')
    expect(wrapper.find('img.preview-thumb').exists()).toBe(false)
  })

  it('enrolls from selected clip frames, reporting partial success', async () => {
    const posted: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        if (url.includes('/api/cameras')) {
          return Promise.resolve(
            jsonResponse([{ camera: 'Front Door', total: 1, size_bytes: 1, today: 0, this_week: 1, last_seen: '' }]),
          )
        }
        if (url.includes('/frames')) {
          return Promise.resolve(jsonResponse({ frames: ['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'] }))
        }
        if (url.includes('/api/clips')) {
          return Promise.resolve(
            jsonResponse([
              {
                id: 'c1',
                camera: 'Front Door',
                file_path: '/data/c1.mp4',
                timestamp: '2026-01-05T10:00:00Z',
                size_bytes: 1000,
                duration: 5,
                source: 'pir',
                network_id: 1,
                starred: false,
                tags: [],
                downloaded_at: '2026-01-05T10:01:00Z',
                archived: false,
                archive_path: '',
                notified: false,
              },
            ]),
          )
        }
        if (init?.method === 'POST') {
          posted.push(JSON.parse(init.body as string).image_base64)
          if (posted.length === 1) return Promise.resolve(jsonResponse({ error: 'No face detected' }, false))
          return Promise.resolve(jsonResponse({ id: 2, name: 'Brian', approved: true }))
        }
        return Promise.resolve(jsonResponse({ available: true, faces: posted.length ? [faceEnrollment()] : [] }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('#biometrics-name').setValue('Brian')
    await flushPromises()

    const frameItems = wrapper.findAll('.frame-item')
    await frameItems[0].trigger('click')
    await frameItems[1].trigger('click')
    await flushPromises()

    const enrollBtn = wrapper.findAll('button').find((b) => b.text().includes('Enroll'))!
    expect(enrollBtn.text()).toContain('2 selected frame')
    await enrollBtn.trigger('click')
    await flushPromises()

    expect(posted).toEqual(['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'])
  })
})
