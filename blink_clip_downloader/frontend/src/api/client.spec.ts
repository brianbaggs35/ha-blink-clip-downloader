import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  api,
  apiDelete,
  ApiError,
  apiGet,
  apiGetWithHeaders,
  apiPatch,
  apiPost,
  apiPut,
  describeApiError,
} from './client'
import { goTo } from '../navigation'

vi.mock('../navigation', () => ({ goTo: vi.fn(), loginUrl: () => '/login?next=%2F' }))

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
    vi.mocked(goTo).mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('api(): returns parsed JSON on success', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
    await expect(api('/api/clips')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledWith('/api/clips', {})
  })

  it('api(): an expired direct-port sign-in sends the browser to the login page', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ error: 'Sign in', login_required: true }, false, 401, 'Unauthorized'),
    )
    await expect(api('/api/clips')).rejects.toMatchObject({ status: 401 })
    expect(goTo).toHaveBeenCalledWith('/login?next=%2F')
  })

  it.each([
    ['a 401 without the sign-in flag', jsonResponse({ error: 'nope' }, false, 401)],
    ['a 401 whose body is not JSON', jsonResponse('Unauthorized', false, 401)],
    ['a 401 whose body is JSON null', jsonResponse('null', false, 401)],
    ['a 403 carrying the flag', jsonResponse({ login_required: true }, false, 403)],
  ])('api(): %s does not navigate', async (_label, response) => {
    vi.mocked(fetch).mockResolvedValue(response)
    await expect(api('/api/clips')).rejects.toBeInstanceOf(ApiError)
    expect(goTo).not.toHaveBeenCalled()
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

  it('apiGetWithHeaders(): returns parsed JSON and response headers', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ...jsonResponse([]),
      headers: new Headers({ ETag: 'revision' }),
    } as Response)
    const response = await apiGetWithHeaders('/api/cameras')
    expect(response.data).toEqual([])
    expect(response.headers.get('ETag')).toBe('revision')
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

  it('apiPut(): sends extra headers when no body is given', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiPut('/api/ai/camera-configs', undefined, { 'If-Match': '"revision"' })
    expect(fetch).toHaveBeenCalledWith('/api/ai/camera-configs', {
      method: 'PUT',
      headers: { 'If-Match': '"revision"' },
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

  it('apiDelete(): sends a JSON body when given one', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await apiDelete('/api/ai/faces/people', { name: 'Mom/Dad' })
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/people', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Mom/Dad' }),
    })
  })

  it('ApiError carries status and message', () => {
    const err = new ApiError(403, '403: Forbidden')
    expect(err.status).toBe(403)
    expect(err.message).toBe('403: Forbidden')
    expect(err.name).toBe('ApiError')
    expect(err).toBeInstanceOf(Error)
  })

  it('describeApiError(): prefers the reason the server gave', () => {
    const fallback = 'Something went wrong'
    const err = (body: string) => new ApiError(400, `400: ${body}`, body)
    expect(describeApiError(err(JSON.stringify({ error: 'Name too long' })), fallback)).toBe('Name too long')
    expect(describeApiError(err('Clip not found'), fallback)).toBe('Clip not found')
    expect(describeApiError(err('  '), fallback)).toBe(fallback)
    expect(describeApiError(err('<html>502 Bad Gateway</html>'), fallback)).toBe(fallback)
    expect(describeApiError(err(JSON.stringify({ detail: 'x' })), fallback)).toBe(fallback)
    expect(describeApiError(err(JSON.stringify({ error: '' })), fallback)).toBe(fallback)
    expect(describeApiError(err('null'), fallback)).toBe(fallback)
    expect(describeApiError(err('x'.repeat(201)), fallback)).toBe(fallback)
    expect(describeApiError(new ApiError(500, '500: '), fallback)).toBe(fallback)
    expect(describeApiError(new TypeError('Failed to fetch'), fallback)).toBe(fallback)
  })

  it('keeps the response body on the error it throws', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      text: () => Promise.resolve('{"error":"nope"}'),
    } as Response)
    await expect(apiGet('/x')).rejects.toMatchObject({ status: 400, body: '{"error":"nope"}' })
  })
})
