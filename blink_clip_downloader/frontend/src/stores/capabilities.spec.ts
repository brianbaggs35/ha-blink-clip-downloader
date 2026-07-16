import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useCapabilitiesStore } from './capabilities'

describe('useCapabilitiesStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts unknown (null) and can be set', () => {
    const store = useCapabilitiesStore()
    expect(store.faceRecognitionAvailable).toBeNull()
    store.setFaceRecognitionAvailable(true)
    expect(store.faceRecognitionAvailable).toBe(true)
    store.setFaceRecognitionAvailable(false)
    expect(store.faceRecognitionAvailable).toBe(false)
  })
})
