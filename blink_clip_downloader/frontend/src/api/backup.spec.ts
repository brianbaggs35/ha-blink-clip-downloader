import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  backupToGDrive,
  createBackup,
  discardBackupUpload,
  getBackupPreview,
  inspectBackup,
  restoreBackup,
  uploadBackupFile,
} from './backup'
import { ApiError } from './client'

function reply(body: unknown, status = 200): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  return {
    ok: status < 400,
    status,
    statusText: status < 400 ? 'OK' : 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  } as Response
}

const noWait = () => Promise.resolve()

/** Every call made, as `METHOD url`, in order. */
function calls(): string[] {
  return vi.mocked(fetch).mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url as string}`)
}

function file(bytes: number): File {
  return new File([new Uint8Array(bytes)], 'backup.zip', { type: 'application/zip' })
}

describe('backup api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('asks what a backup would hold', async () => {
    vi.mocked(fetch).mockResolvedValue(reply({ tables: [] }))
    await expect(getBackupPreview()).resolves.toEqual({ tables: [] })
    expect(calls()).toEqual(['GET /api/backup/preview'])
  })

  describe('createBackup', () => {
    it('returns the file under the name the server gave it', async () => {
      const blob = new Blob(['zip'])
      vi.mocked(fetch).mockResolvedValue({
        ...reply({}),
        blob: () => Promise.resolve(blob),
        headers: new Headers({ 'Content-Disposition': 'attachment; filename="blink-clips-backup-1.zip"' }),
      } as Response)
      const made = await createBackup(false)
      expect(made).toEqual({ blob, filename: 'blink-clips-backup-1.zip' })
      expect(vi.mocked(fetch).mock.calls[0][1]?.body).toBe(JSON.stringify({ include_faces: false }))
    })

    it('falls back to a plain name when the server gave none', async () => {
      vi.mocked(fetch).mockResolvedValue({
        ...reply({}),
        blob: () => Promise.resolve(new Blob([])),
        headers: new Headers(),
      } as Response)
      expect((await createBackup(true)).filename).toBe('blink-clips-backup.zip')
    })

    it('fails with the reason the server gave', async () => {
      vi.mocked(fetch).mockResolvedValue(reply('Creating the backup failed: No space left on device.', 500))
      await expect(createBackup(true)).rejects.toMatchObject({
        status: 500,
        body: 'Creating the backup failed: No space left on device.',
      })
    })
  })

  it('backs up to Google Drive, saying whether to include faces', async () => {
    vi.mocked(fetch).mockResolvedValue(reply({ name: 'x.zip' }))
    await backupToGDrive(false)
    expect(calls()).toEqual(['POST /api/backup/gdrive'])
    expect(vi.mocked(fetch).mock.calls[0][1]?.body).toBe(JSON.stringify({ include_faces: false }))
  })

  describe('uploadBackupFile', () => {
    function serverTakingPieces(chunkBytes: number) {
      vi.mocked(fetch).mockImplementation((url, init) => {
        const target = url as string
        if (init?.method === 'POST') return Promise.resolve(reply({ upload_id: 'u1', chunk_bytes: chunkBytes }))
        if (init?.method === 'PUT') {
          const offset = Number(new URL(target, 'http://x').searchParams.get('offset'))
          return Promise.resolve(reply({ received: offset + (init.body as Blob).size }))
        }
        return Promise.resolve(reply({ discarded: true }))
      })
    }

    it('sends the file in order, a piece at a time, and says how far it got', async () => {
      serverTakingPieces(4)
      const progress: Array<[number, number]> = []

      const id = await uploadBackupFile(file(10), (sent, total) => progress.push([sent, total]), undefined, noWait)

      expect(id).toBe('u1')
      expect(calls()).toEqual([
        'POST /api/backup/uploads',
        'PUT /api/backup/uploads/u1?offset=0',
        'PUT /api/backup/uploads/u1?offset=4',
        'PUT /api/backup/uploads/u1?offset=8',
      ])
      expect(progress).toEqual([
        [4, 10],
        [8, 10],
        [10, 10],
      ])
      const sizes = vi
        .mocked(fetch)
        .mock.calls.slice(1)
        .map(([, init]) => (init?.body as Blob).size)
      expect(sizes).toEqual([4, 4, 2])
    })

    it('says how big the file is when it starts', async () => {
      serverTakingPieces(100)
      await uploadBackupFile(file(10), () => undefined, undefined, noWait)
      expect(vi.mocked(fetch).mock.calls[0][1]?.body).toBe(JSON.stringify({ size: 10 }))
    })

    it('tries a piece again after the connection drops', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      let dropped = false
      vi.mocked(fetch).mockImplementation((url, init) => {
        if (init?.method === 'PUT' && !dropped) {
          dropped = true
          return Promise.reject(new TypeError('Failed to fetch'))
        }
        return sent(url, init)
      })
      const waits: number[] = []

      await uploadBackupFile(
        file(10),
        () => undefined,
        undefined,
        (ms) => {
          waits.push(ms)
          return Promise.resolve()
        },
      )

      expect(calls().filter((c) => c.startsWith('PUT'))).toHaveLength(2)
      expect(waits).toEqual([1000])
    })

    it('tries a piece again after the proxy in front of the add-on has a bad moment', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      let failures = 2
      vi.mocked(fetch).mockImplementation((url, init) => {
        if (init?.method === 'PUT' && failures-- > 0) return Promise.resolve(reply('Bad gateway', 502))
        return sent(url, init)
      })
      const waits: number[] = []

      await uploadBackupFile(
        file(10),
        () => undefined,
        undefined,
        (ms) => {
          waits.push(ms)
          return Promise.resolve()
        },
      )

      expect(waits).toEqual([1000, 2000])
    })

    it('gives up after three tries and throws the piece-sized failure away', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      vi.mocked(fetch).mockImplementation((url, init) =>
        init?.method === 'PUT' ? Promise.reject(new TypeError('Failed to fetch')) : sent(url, init),
      )

      await expect(uploadBackupFile(file(10), () => undefined, undefined, noWait)).rejects.toBeInstanceOf(TypeError)

      expect(calls().filter((c) => c.startsWith('PUT'))).toHaveLength(3)
      // And the half-sent file does not stay on the add-on.
      expect(calls().at(-1)).toBe('DELETE /api/backup/uploads/u1')
    })

    it('does not retry a refusal that would only be repeated', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      vi.mocked(fetch).mockImplementation((url, init) =>
        init?.method === 'PUT' ? Promise.resolve(reply('That part does not fit', 400)) : sent(url, init),
      )

      await expect(uploadBackupFile(file(10), () => undefined, undefined, noWait)).rejects.toMatchObject({
        status: 400,
      })

      expect(calls().filter((c) => c.startsWith('PUT'))).toHaveLength(1)
      expect(calls().at(-1)).toBe('DELETE /api/backup/uploads/u1')
    })

    it('carries on from where the server says it is when a reply was lost', async () => {
      serverTakingPieces(4)
      const sent = vi.mocked(fetch).getMockImplementation()!
      let lost = false
      vi.mocked(fetch).mockImplementation((url, init) => {
        const offset = Number(new URL(url as string, 'http://x').searchParams.get('offset'))
        if (init?.method === 'PUT' && offset === 0 && !lost) {
          lost = true
          // The server stored the first piece, but the answer never arrived.
          return Promise.reject(new TypeError('Failed to fetch'))
        }
        if (init?.method === 'PUT' && offset === 0) {
          return Promise.resolve(reply({ error: 'out of order', received: 4 }, 409))
        }
        return sent(url, init)
      })

      await uploadBackupFile(file(10), () => undefined, undefined, noWait)

      const offsets = calls()
        .filter((c) => c.startsWith('PUT'))
        .map((c) => c.split('offset=')[1])
      expect(offsets).toEqual(['0', '0', '4', '8'])
    })

    it.each([
      ['a body that is not JSON', 'conflict'],
      ['JSON that does not say where the server is', JSON.stringify({ error: 'conflict' })],
      ['JSON null', 'null'],
    ])('a conflict with %s is a real failure, not a place to resume', async (_label, body) => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      vi.mocked(fetch).mockImplementation((url, init) =>
        init?.method === 'PUT' ? Promise.resolve(reply(body, 409)) : sent(url, init),
      )
      await expect(uploadBackupFile(file(10), () => undefined, undefined, noWait)).rejects.toBeInstanceOf(ApiError)
    })

    it('stops when asked to, and cleans up', async () => {
      serverTakingPieces(4)
      const controller = new AbortController()
      const progress = vi.fn((sent: number) => {
        if (sent >= 4) controller.abort()
      })

      await expect(uploadBackupFile(file(10), progress, controller.signal, noWait)).rejects.toMatchObject({
        name: 'AbortError',
      })

      expect(calls().filter((c) => c.startsWith('PUT'))).toHaveLength(1)
      expect(calls().at(-1)).toBe('DELETE /api/backup/uploads/u1')
    })

    it('does not retry a piece that failed because the upload was cancelled', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      const controller = new AbortController()
      vi.mocked(fetch).mockImplementation((url, init) => {
        if (init?.method !== 'PUT') return sent(url, init)
        controller.abort()
        return Promise.reject(new TypeError('aborted'))
      })

      await expect(uploadBackupFile(file(10), () => undefined, controller.signal, noWait)).rejects.toBeInstanceOf(
        TypeError,
      )

      expect(calls().filter((c) => c.startsWith('PUT'))).toHaveLength(1)
    })

    it('reports the original failure even if cleaning up fails too', async () => {
      serverTakingPieces(100)
      const sent = vi.mocked(fetch).getMockImplementation()!
      vi.mocked(fetch).mockImplementation((url, init) => {
        if (init?.method === 'PUT') return Promise.resolve(reply('Bad request', 400))
        if (init?.method === 'DELETE') return Promise.reject(new TypeError('offline'))
        return sent(url, init)
      })
      await expect(uploadBackupFile(file(10), () => undefined, undefined, noWait)).rejects.toMatchObject({
        status: 400,
      })
    })

    it('does not start sending when the add-on will not take the file', async () => {
      vi.mocked(fetch).mockResolvedValue(reply({ error: 'too large' }, 413))
      await expect(uploadBackupFile(file(10), () => undefined, undefined, noWait)).rejects.toMatchObject({
        status: 413,
      })
      expect(calls()).toEqual(['POST /api/backup/uploads'])
    })

    it('waits for real when no waiter is given', async () => {
      vi.useFakeTimers()
      try {
        serverTakingPieces(100)
        const sent = vi.mocked(fetch).getMockImplementation()!
        let dropped = false
        vi.mocked(fetch).mockImplementation((url, init) => {
          if (init?.method === 'PUT' && !dropped) {
            dropped = true
            return Promise.reject(new TypeError('Failed to fetch'))
          }
          return sent(url, init)
        })
        const done = uploadBackupFile(file(10), () => undefined)
        await vi.advanceTimersByTimeAsync(1000)
        await expect(done).resolves.toBe('u1')
      } finally {
        vi.useRealTimers()
      }
    })
  })

  it('has the add-on look over the uploaded file', async () => {
    vi.mocked(fetch).mockResolvedValue(reply({ verified_parts: 3 }))
    await expect(inspectBackup('u1')).resolves.toEqual({ verified_parts: 3 })
    expect(calls()).toEqual(['POST /api/backup/uploads/u1/inspect'])
  })

  it('restores, saying whether settings that exist should be replaced', async () => {
    vi.mocked(fetch).mockResolvedValue(reply({ verified: true }))
    await restoreBackup('u1', true)
    expect(calls()).toEqual(['POST /api/backup/uploads/u1/restore'])
    expect(vi.mocked(fetch).mock.calls[0][1]?.body).toBe(JSON.stringify({ replace_settings: true }))
  })

  it('throws away an upload', async () => {
    vi.mocked(fetch).mockResolvedValue(reply({ discarded: true }))
    await discardBackupUpload('u1')
    expect(calls()).toEqual(['DELETE /api/backup/uploads/u1'])
  })
})
