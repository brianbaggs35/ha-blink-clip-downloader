import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Paginator from 'primevue/paginator'
import Select from 'primevue/select'
import ArchivedClipsSection from './ArchivedClipsSection.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'
import type { ArchiveGroup, ClipListItem } from '../../api/types'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function makeGroup(overrides: Partial<ArchiveGroup> = {}): ArchiveGroup {
  return {
    archive_path: '/data/archives/2026-06.zip',
    clip_count: 2,
    total_size: 9_000_000,
    latest_timestamp: '2026-06-15T10:00:00Z',
    ...overrides,
  }
}

function makeClip(overrides: Partial<ClipListItem> = {}): ClipListItem {
  return {
    id: 'a1',
    camera: 'Front Door',
    file_path: '/data/archives/2026-06.zip',
    timestamp: '2026-06-05T10:00:00Z',
    size_bytes: 5_000_000,
    duration: 30,
    source: 'pir',
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '2026-06-05T10:01:00Z',
    archived: true,
    archive_path: '/data/archives/2026-06.zip',
    gdrive_backed_up: false,
    gdrive_file_id: '',
    gdrive_uploaded_at: '',
    notified: false,
    face_recognized: false,
    ...overrides,
  }
}

interface Routes {
  groups?: ArchiveGroup[]
  groupsFail?: boolean
  cameras?: string[]
  camerasFail?: boolean
  clips?: Record<string, ClipListItem[]>
  clipsFail?: Record<string, boolean>
  deleteFail?: boolean
  runNowArchived?: number
  runNowFail?: boolean
}

function routedFetch(routes: Routes) {
  return vi.fn((url: string, opts?: RequestInit) => {
    if (opts?.method === 'DELETE') {
      if (routes.deleteFail) return Promise.reject(new Error('down'))
      return Promise.resolve(jsonResponse({ deleted: true, gdrive_deleted: null }))
    }
    if (url === '/api/storage/archive/run-now' && opts?.method === 'POST') {
      if (routes.runNowFail) return Promise.reject(new Error('down'))
      return Promise.resolve(jsonResponse({ archived: routes.runNowArchived ?? 0 }))
    }
    if (url.startsWith('/api/storage/archives')) {
      if (routes.groupsFail) return Promise.reject(new Error('down'))
      return Promise.resolve(jsonResponse(routes.groups ?? []))
    }
    if (url.startsWith('/api/storage/archive-clips')) {
      const params = new URL(url, 'http://localhost').searchParams
      const archivePath = params.get('archive_path') ?? ''
      if (routes.clipsFail?.[archivePath]) return Promise.reject(new Error('down'))
      const clips = routes.clips?.[archivePath] ?? []
      const offset = Number(params.get('offset') ?? 0)
      const limit = Number(params.get('limit') ?? 50)
      return Promise.resolve(jsonResponse({ items: clips.slice(offset, offset + limit), total: clips.length }))
    }
    if (url.startsWith('/api/cameras')) {
      if (routes.camerasFail) return Promise.reject(new Error('down'))
      return Promise.resolve(
        jsonResponse(
          (routes.cameras ?? []).map((c) => ({
            camera: c,
            total: 0,
            size_bytes: 0,
            today: 0,
            this_week: 0,
            last_seen: '',
          })),
        ),
      )
    }
    return Promise.resolve(jsonResponse([]))
  })
}

function mountSection() {
  return mount(ArchivedClipsSection, { global: { plugins: [PrimeVue] } })
}

async function expandFirstArchive(wrapper: VueWrapper) {
  await wrapper.find('.archive-panel-header').trigger('click')
  await flushPromises()
}

describe('ArchivedClipsSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows a loading indicator while the initial fetch is pending', async () => {
    let resolveFetch: (v: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve
          }),
      ),
    )
    const wrapper = mountSection()
    expect(wrapper.find('.archived-status').exists()).toBe(true)
    resolveFetch(jsonResponse([]))
    await flushPromises()
  })

  it('shows an empty state with no filters active', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [] }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('No archived clips yet.')
  })

  it('shows a load error', async () => {
    vi.stubGlobal('fetch', routedFetch({ groupsFail: true }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load archived clips.')
  })

  it('fetches archive groups from /api/storage/archives on mount', async () => {
    const fetchMock = routedFetch({ groups: [] })
    vi.stubGlobal('fetch', fetchMock)
    mountSection()
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/storage/archives', {})
  })

  it('renders a group as a ZIP-tagged panel with clip count and size', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [makeGroup()] }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('ZIP')
    expect(wrapper.text()).toContain('2026-06.zip')
    expect(wrapper.text()).toContain('2 clips')
    expect(wrapper.text()).toContain('8.6 MB')
  })

  it('singularizes the clip count for a one-clip archive', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [makeGroup({ clip_count: 1 })] }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('1 clip')
    expect(wrapper.text()).not.toContain('1 clips')
  })

  it('populates the camera filter dropdown from getCameras()', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [], cameras: ['Front Door', 'Driveway'] }))
    const wrapper = mountSection()
    await flushPromises()
    const select = wrapper.findComponent(Select)
    expect(select.props('options')).toEqual(['all', 'Driveway', 'Front Door'])
  })

  it('refetches with a camera filter and shows the Clear filters button', async () => {
    const fetchMock = routedFetch({ groups: [], cameras: ['Front Door'] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    expect(wrapper.text()).not.toContain('Clear filters')
    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    const url = fetchMock.mock.calls.at(-1)?.[0] as string
    expect(url).toContain('/api/storage/archives?camera=Front+Door')
    expect(wrapper.text()).toContain('Clear filters')
  })

  it('clears filters and refetches unfiltered', async () => {
    const fetchMock = routedFetch({ groups: [] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()
    await wrapper.find('button').trigger('click') // "Clear filters" is the only Button rendered at this point
    await flushPromises()

    expect(wrapper.text()).not.toContain('Clear filters')
    const url = fetchMock.mock.calls.at(-1)?.[0] as string
    expect(url).toBe('/api/storage/archives')
  })

  it('shows the filtered-empty message once a filter is active and nothing matches', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [] }))
    const wrapper = mountSection()
    await flushPromises()
    await wrapper.find('input[type="date"]').setValue('2026-06-01')
    await flushPromises()
    expect(wrapper.text()).toContain('No archives match these filters.')
  })

  it('treats a left-only date as one inclusive calendar day', async () => {
    const fetchMock = routedFetch({ groups: [] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    await wrapper.findAll('input[type="date"]')[0].setValue('2026-06-01')
    await flushPromises()

    const url = fetchMock.mock.calls.at(-1)?.[0] as string
    expect(url).toContain('since=2026-06-01T00%3A00%3A00%2B00%3A00')
    expect(url).toContain('until=2026-06-01T23%3A59%3A59.999999%2B00%3A00')
  })

  it('sends an inclusive date range when both dates are selected', async () => {
    const fetchMock = routedFetch({ groups: [] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const dateInputs = wrapper.findAll('input[type="date"]')
    await dateInputs[0].setValue('2026-06-01')
    await dateInputs[1].setValue('2026-06-30')
    await flushPromises()

    const url = fetchMock.mock.calls.at(-1)?.[0] as string
    expect(url).toContain('since=2026-06-01T00%3A00%3A00%2B00%3A00')
    expect(url).toContain('until=2026-06-30T23%3A59%3A59.999999%2B00%3A00')
  })

  it('expands an archive and fetches its clips, grouped by camera', async () => {
    const group = makeGroup()
    const clips = [makeClip({ id: 'c1', camera: 'Front Door' }), makeClip({ id: 'c2', camera: 'Driveway' })]
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: clips } }))
    const wrapper = mountSection()
    await flushPromises()

    await expandFirstArchive(wrapper)

    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Not backed up')
  })

  it('passes the active camera filter into the per-archive clip fetch', async () => {
    const group = makeGroup()
    const fetchMock = routedFetch({
      groups: [group],
      cameras: ['Front Door'],
      clips: { [group.archive_path]: [makeClip()] },
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    await expandFirstArchive(wrapper)

    const clipsCall = fetchMock.mock.calls.find((c) => (c[0] as string).startsWith('/api/storage/archive-clips'))
    expect(clipsCall?.[0]).toContain('camera=Front+Door')
    expect(clipsCall?.[0]).toContain(`archive_path=${encodeURIComponent(group.archive_path)}`)
  })

  it('does not re-fetch clips when re-expanding an already-loaded archive', async () => {
    const group = makeGroup()
    const fetchMock = routedFetch({ groups: [group], clips: { [group.archive_path]: [makeClip()] } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    await expandFirstArchive(wrapper) // expand (fetches)
    await expandFirstArchive(wrapper) // collapse
    await expandFirstArchive(wrapper) // re-expand (should use cache)

    const clipCalls = fetchMock.mock.calls.filter((c) => (c[0] as string).startsWith('/api/storage/archive-clips'))
    expect(clipCalls).toHaveLength(1)
  })

  it('shows an error if the per-archive clip fetch fails', async () => {
    const group = makeGroup()
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clipsFail: { [group.archive_path]: true } }))
    const wrapper = mountSection()
    await flushPromises()

    await expandFirstArchive(wrapper)

    expect(wrapper.text()).toContain('Failed to load clips in this archive.')
  })

  it('deletes a clip after confirmation and decrements the group count', async () => {
    const group = makeGroup({ clip_count: 2 })
    const clips = [makeClip({ id: 'c1' }), makeClip({ id: 'c2' })]
    let deleted = false
    const baseFetch = routedFetch({ groups: [group], clips: { [group.archive_path]: clips } })
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (options?.method === 'DELETE') deleted = true
      if (deleted && url.startsWith('/api/storage/archive-clips')) {
        return Promise.resolve(jsonResponse({ items: [clips[1]], total: 1 }))
      }
      return baseFetch(url, options)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const confirm = useConfirmStore()
    const deleteButton = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    const clickPromise = deleteButton!.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/clips/c1', { method: 'DELETE' })
    expect(wrapper.text()).toContain('1 clip')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Delete')).toHaveLength(1)
  })

  it('removes the whole group once its last clip is deleted', async () => {
    const group = makeGroup({ clip_count: 1 })
    const clip = makeClip({ id: 'only' })
    const fetchMock = routedFetch({ groups: [group], clips: { [group.archive_path]: [clip] } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(wrapper.text()).toContain('No archived clips yet.')
  })

  it('does not delete when the confirmation is declined', async () => {
    const group = makeGroup()
    const fetchMock = routedFetch({ groups: [group], clips: { [group.archive_path]: [makeClip()] } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalledWith('/api/clips/a1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('shows an error toast when delete fails', async () => {
    const group = makeGroup()
    const fetchMock = routedFetch({
      groups: [group],
      clips: { [group.archive_path]: [makeClip()] },
      deleteFail: true,
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(useToastStore().message).toBe('Could not delete clip')
    expect(useToastStore().isError).toBe(true)
  })

  it('mentions the Google Drive backup in the confirmation for a backed-up clip', async () => {
    const group = makeGroup()
    const clip = makeClip({ gdrive_backed_up: true })
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: [clip] } }))
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    expect(confirm.message).toContain('Google Drive backup')
    confirm.settle(false)
    await clickPromise
    await flushPromises()
  })

  it('hides the paginator at or below one page of groups', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [makeGroup()] }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.findComponent(Paginator).exists()).toBe(false)
  })

  it('paginates groups once there are more than one page', async () => {
    const groups = Array.from({ length: 15 }, (_, i) =>
      makeGroup({ archive_path: `/data/archives/2026-${String(i + 1).padStart(2, '0')}.zip` }),
    )
    vi.stubGlobal('fetch', routedFetch({ groups }))
    const wrapper = mountSection()
    await flushPromises()

    const paginator = wrapper.findComponent(Paginator)
    expect(paginator.exists()).toBe(true)
    expect(paginator.props('totalRecords')).toBe(15)
    expect(paginator.props('rows')).toBe(10)
    expect(wrapper.findAll('.archive-panel')).toHaveLength(10)

    await paginator.vm.$emit('update:first', 10)
    await flushPromises()
    expect(wrapper.findAll('.archive-panel')).toHaveLength(5)
  })

  it('paginates an expanded archive in 50-clip pages without loading all clips', async () => {
    const group = makeGroup({ clip_count: 51 })
    const clips = Array.from({ length: 51 }, (_, i) => makeClip({ id: `clip-${i}` }))
    const fetchMock = routedFetch({ groups: [group], clips: { [group.archive_path]: clips } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    await expandFirstArchive(wrapper)

    const paginator = wrapper.find('.archive-clips-paginator')
    expect(paginator.exists()).toBe(true)
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Delete')).toHaveLength(50)
    const firstCall = fetchMock.mock.calls.find((call) =>
      (call[0] as string).startsWith('/api/storage/archive-clips'),
    )?.[0] as string
    expect(firstCall).toContain('limit=50')
    expect(firstCall).toContain('offset=0')

    await paginator.find('.p-paginator-next').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('button').filter((b) => b.text() === 'Delete')).toHaveLength(1)
    const archiveCalls = fetchMock.mock.calls
      .filter((call) => (call[0] as string).startsWith('/api/storage/archive-clips'))
      .map((call) => call[0] as string)
    expect(archiveCalls.at(-1)).toContain('offset=50')
  })

  it('moves back to the previous clip page after deleting its last clip', async () => {
    const group = makeGroup({ clip_count: 51 })
    const clips = Array.from({ length: 51 }, (_, i) => makeClip({ id: `clip-${i}` }))
    const fetchMock = routedFetch({ groups: [group], clips: { [group.archive_path]: clips } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)
    await wrapper.find('.archive-clips-paginator .p-paginator-next').trigger('click')
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((button) => button.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(wrapper.findAll('button').filter((button) => button.text() === 'Delete')).toHaveLength(50)
  })

  it('reuses the top-level camera and date filters for expanded archive pages', async () => {
    const group = makeGroup()
    const fetchMock = routedFetch({
      groups: [group],
      cameras: ['Front Door'],
      clips: { [group.archive_path]: [makeClip()] },
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()
    const dateInputs = wrapper.findAll('input[type="date"]')
    await dateInputs[0].setValue('2026-06-01')
    await dateInputs[1].setValue('2026-06-30')
    await flushPromises()
    await expandFirstArchive(wrapper)

    const url = fetchMock.mock.calls
      .filter((call) => (call[0] as string).startsWith('/api/storage/archive-clips'))
      .at(-1)?.[0] as string
    expect(url).toContain('camera=Front+Door')
    expect(url).toContain('since=2026-06-01T00%3A00%3A00%2B00%3A00')
    expect(url).toContain('until=2026-06-30T23%3A59%3A59.999999%2B00%3A00')
    expect(url).toContain('offset=0')
  })

  it('ignores an archive page response that belongs to older filters', async () => {
    const group = makeGroup()
    let resolveClipRequest: (response: Response) => void = () => {}
    let clipRequestPending = true
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith('/api/storage/archives')) {
        return Promise.resolve(jsonResponse([group]))
      }
      if (url.startsWith('/api/storage/archive-clips') && clipRequestPending) {
        clipRequestPending = false
        return new Promise<Response>((resolve) => {
          resolveClipRequest = resolve
        })
      }
      if (url.startsWith('/api/storage/archive-clips')) {
        return Promise.resolve(jsonResponse({ items: [], total: 0 }))
      }
      if (url.startsWith('/api/cameras')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse([]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    await wrapper.find('.archive-panel-header').trigger('click')
    await wrapper.findAll('input[type="date"]')[0].setValue('2026-06-01')
    await flushPromises()

    resolveClipRequest(jsonResponse({ items: [makeClip()], total: 1 }))
    await flushPromises()

    expect(wrapper.text()).not.toContain('Not backed up')
  })

  it('keeps the expanded archive body at a stable, theme-aware size', async () => {
    const group = makeGroup()
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: [makeClip()] } }))
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    const content = wrapper.find('.archive-clips-content')
    expect(content.exists()).toBe(true)
    expect(content.classes()).toContain('archive-clips-content')
  })

  it('shows an empty matching state for a filtered archive', async () => {
    const group = makeGroup()
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: [] } }))
    const wrapper = mountSection()
    await flushPromises()
    await expandFirstArchive(wrapper)

    expect(wrapper.text()).toContain('No clips in this archive match the active filters.')
  })

  it('exposes reload() for the parent to call', async () => {
    const fetchMock = routedFetch({ groups: [] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    const callsBefore = fetchMock.mock.calls.length
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('toggles an archive open via keyboard (Enter/Space), not just a click', async () => {
    const group = makeGroup()
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: [makeClip()] } }))
    const wrapper = mountSection()
    await flushPromises()

    const header = wrapper.find('.archive-panel-header')
    expect(header.attributes('aria-expanded')).toBe('false')

    await header.trigger('keydown.enter')
    await flushPromises()
    expect(header.attributes('aria-expanded')).toBe('true')
    expect(wrapper.text()).toContain('Not backed up')

    await header.trigger('keydown.space')
    await flushPromises()
    expect(header.attributes('aria-expanded')).toBe('false')
  })

  it("toggles via PrimeVue's own Panel toggle button, not just the custom header", async () => {
    const group = makeGroup()
    vi.stubGlobal('fetch', routedFetch({ groups: [group], clips: { [group.archive_path]: [makeClip()] } }))
    const wrapper = mountSection()
    await flushPromises()

    await wrapper.find('.p-panel-toggle-button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Not backed up')
  })

  it('treats an archive with no path as "Unknown archive"', async () => {
    const group = makeGroup({ archive_path: '' })
    vi.stubGlobal('fetch', routedFetch({ groups: [group] }))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('Unknown archive')
  })

  it('steps the paginator back a page if deletion empties the current page', async () => {
    const groups = Array.from({ length: 11 }, (_, i) =>
      makeGroup({ archive_path: `/data/archives/2026-${String(i + 1).padStart(2, '0')}.zip`, clip_count: 1 }),
    )
    const lastGroup = groups[10]
    const fetchMock = routedFetch({ groups, clips: { [lastGroup.archive_path]: [makeClip({ id: 'only' })] } })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const paginator = wrapper.findComponent(Paginator)
    await paginator.vm.$emit('update:first', 10)
    await flushPromises()
    expect(wrapper.findAll('.archive-panel')).toHaveLength(1)

    await expandFirstArchive(wrapper)
    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    // Back down to exactly one page's worth of groups (10) - the paginator
    // disappears entirely, and all 10 render, proving `first` stepped back
    // to 0 rather than staying at 10 (which would otherwise slice to empty).
    expect(wrapper.findComponent(Paginator).exists()).toBe(false)
    expect(wrapper.findAll('.archive-panel')).toHaveLength(10)
  })

  it('runs archiving now, toasts the result, and reloads the archive list', async () => {
    const groupsBefore = [makeGroup()]
    const groupsAfter = [makeGroup(), makeGroup({ archive_path: '/data/archives/2026-07.zip' })]
    let loadCount = 0
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/archive/run-now' && opts?.method === 'POST')
        return Promise.resolve(jsonResponse({ archived: 3 }))
      if (url.startsWith('/api/storage/archives')) {
        loadCount += 1
        return Promise.resolve(jsonResponse(loadCount === 1 ? groupsBefore : groupsAfter))
      }
      if (url.startsWith('/api/cameras')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse([]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.findAll('.archive-panel')).toHaveLength(1)

    const toast = useToastStore()
    const runNowBtn = wrapper.findAll('button').find((b) => b.text() === 'Run Archiving Now')
    await runNowBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/archive/run-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
    expect(toast.message).toBe('Archived 3 clip(s)')
    expect(wrapper.findAll('.archive-panel')).toHaveLength(2)
  })

  it('shows an error toast when running archiving now fails', async () => {
    vi.stubGlobal('fetch', routedFetch({ groups: [makeGroup()], runNowFail: true }))
    const wrapper = mountSection()
    await flushPromises()

    const toast = useToastStore()
    const runNowBtn = wrapper.findAll('button').find((b) => b.text() === 'Run Archiving Now')
    await runNowBtn!.trigger('click')
    await flushPromises()

    expect(toast.message).toBe('Could not run archiving')
    expect(toast.isError).toBe(true)
  })
})
