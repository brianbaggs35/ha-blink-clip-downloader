import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { photoDataUrl } from './photo'

const ORIGINAL = 'data:image/jpeg;base64,ORIGINAL'

class FakeFileReader {
  static fail = false
  result: string | null = null
  error: Error | null = null
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  readAsDataURL() {
    queueMicrotask(() => {
      if (FakeFileReader.fail) {
        this.onerror?.()
      } else {
        this.result = ORIGINAL
        this.onload?.()
      }
    })
  }
}

function stubBitmap(width: number, height: number) {
  const bitmap = { width, height, close: vi.fn() }
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue(bitmap))
  return bitmap
}

const file = new File(['x'], 'me.jpg', { type: 'image/jpeg' })

describe('photoDataUrl', () => {
  beforeEach(() => {
    FakeFileReader.fail = false
    vi.stubGlobal('FileReader', FakeFileReader)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('shrinks a large photo to the size the server looks at', async () => {
    const bitmap = stubBitmap(4000, 3000)
    const drawImage = vi.fn()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,SMALL')

    expect(await photoDataUrl(file)).toBe('data:image/jpeg;base64,SMALL')
    expect(drawImage).toHaveBeenCalledWith(bitmap, 0, 0, 1280, 960)
    expect(bitmap.close).toHaveBeenCalled()
  })

  it('sends a photo that is already small unchanged', async () => {
    const bitmap = stubBitmap(800, 600)
    expect(await photoDataUrl(file)).toBe(ORIGINAL)
    expect(bitmap.close).toHaveBeenCalled()
  })

  it('sends the original when the browser cannot decode it', async () => {
    vi.stubGlobal('createImageBitmap', vi.fn().mockRejectedValue(new Error('HEIC')))
    expect(await photoDataUrl(file)).toBe(ORIGINAL)
  })

  it('sends the original when no canvas is available', async () => {
    stubBitmap(4000, 3000)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    expect(await photoDataUrl(file)).toBe(ORIGINAL)
  })

  it('rejects when the file cannot be read at all', async () => {
    FakeFileReader.fail = true
    vi.stubGlobal('createImageBitmap', vi.fn().mockRejectedValue(new Error('nope')))
    await expect(photoDataUrl(file)).rejects.toThrow('Could not read the file')
  })
})
