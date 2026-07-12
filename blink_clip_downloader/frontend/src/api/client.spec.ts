import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, apiDelete, ApiError, apiGet, apiPatch, apiPost, apiPut } from './client'

function jsonResponse(body: unknown, ok = true, status = 200, statusText = 'OK') {
  return {
    ok,
    status,
    statusText,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

describe('api client', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('api(): returns parsed JSON on success', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
    await expect(api('/api/clips')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledWith('/api/clips', {})
  })

  it('api(): throws ApiError with status + body text on failure', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse('nope', false, 404, 'Not Found'))
    await expect(api('/api/clips/missing')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: '404: nope',
    })
  })

  it('api(): falls back to statusText if reading the error body fails', async () => {
    const res = jsonResponse(undefined, false, 500, 'Server Error')
    res.text = () => Promise.reject(new Error('boom'))
    vi.mocked(fetch).mockResolvedValue(res)
    await expect(api('/api/x')).rejects.toMatchObject({ message: '500: Server Error' })
  })

  it('apiGet(): plain GET, no body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await apiGet('/api/cameras')
    expect(fetch).toHaveBeenCalledWith('/api/cameras', {})
  })

  it('apiPost(): sends a JSON body with the right header', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPost('/api/clips/export-zip', { ids: ['a', 'b'] })
    expect(fetch).toHaveBeenCalledWith('/api/clips/export-zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ['a', 'b'] }),
    })
  })

  it('apiPost(): omits body/headers when no body is given', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPost('/api/download-now')
    expect(fetch).toHaveBeenCalledWith('/api/download-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('apiPut(): sends a JSON body via PUT', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPut('/api/clips/1/star', { starred: true })
    expect(fetch).toHaveBeenCalledWith('/api/clips/1/star', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starred: true }),
    })
  })

  it('apiPut(): omits body/headers when no body is given', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPut('/api/clips/1/tags')
    expect(fetch).toHaveBeenCalledWith('/api/clips/1/tags', {
      method: 'PUT',
      headers: undefined,
      body: undefined,
    })
  })

  it('apiPatch(): sends a JSON body via PATCH', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPatch('/api/ai/faces/1', { approved: false })
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/1', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: false }),
    })
  })

  it('apiPatch(): omits body/headers when no body is given', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPatch('/api/ai/faces/1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/1', {
      method: 'PATCH',
      headers: undefined,
      body: undefined,
    })
  })

  it('apiDelete(): sends a DELETE with no body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiDelete('/api/clips/1')
    expect(fetch).toHaveBeenCalledWith('/api/clips/1', { method: 'DELETE' })
  })

  it('ApiError carries status and message', () => {
    const err = new ApiError(403, '403: Forbidden')
    expect(err.status).toBe(403)
    expect(err.message).toBe('403: Forbidden')
    expect(err.name).toBe('ApiError')
    expect(err).toBeInstanceOf(Error)
  })
})
