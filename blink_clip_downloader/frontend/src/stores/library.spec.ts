import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useLibraryStore } from './library'

describe('useLibraryStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('defaults to no cameras and "all" selected', () => {
    const store = useLibraryStore()
    expect(store.cameras).toEqual([])
    expect(store.currentCamera).toBe('all')
  })

  it('setCameras() replaces the camera list', () => {
    const store = useLibraryStore()
    const cams = [{ camera: 'front', total: 1, size_bytes: 0, today: 0, this_week: 0, last_seen: '' }]
    store.setCameras(cams)
    expect(store.cameras).toEqual(cams)
  })

  it('selectCamera() updates the current filter', () => {
    const store = useLibraryStore()
    store.selectCamera('front')
    expect(store.currentCamera).toBe('front')
  })
})
