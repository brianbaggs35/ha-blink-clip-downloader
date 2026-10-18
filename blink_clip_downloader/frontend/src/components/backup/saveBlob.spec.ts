import { afterEach, describe, expect, it, vi } from 'vitest'
import { saveBlob } from './saveBlob'

describe('saveBlob', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    // @ts-expect-error jsdom has no createObjectURL; the test installs one
    delete URL.createObjectURL
    // @ts-expect-error likewise
    delete URL.revokeObjectURL
  })

  it('offers the blob to the browser as a download with that name', () => {
    URL.createObjectURL = vi.fn(() => 'blob:backup')
    URL.revokeObjectURL = vi.fn()
    const clicked: Array<[string, string]> = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push([this.href, this.download])
    })
    const blob = new Blob(['zip'])

    saveBlob(blob, 'blink-clips-backup.zip')

    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    expect(clicked).toEqual([['blob:backup', 'blink-clips-backup.zip']])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:backup')
  })
})
