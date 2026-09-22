import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  deleteFacePhoto,
  detectFacesInPhoto,
  enrollFaces,
  faceThumbUrl,
  getFaceBypassStats,
  getFaceRecognitionFeedback,
  groupFaces,
  listFaces,
  removePerson,
  scanClipForFaces,
  submitFaceRecognitionFeedback,
  updatePerson,
} from './faces'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: new Headers(),
  } as Response
}

describe('faces api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists enrolled photos', async () => {
    await listFaces()
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces', {})
  })

  it('scans a clip, encoding its id', async () => {
    await scanClipForFaces('local_1/2')
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/scan/local_1%2F2', {})
  })

  it('detects faces in an uploaded photo', async () => {
    await detectFacesInPhoto('data:image/jpeg;base64,abc')
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/detect', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ image_base64: 'data:image/jpeg;base64,abc' }),
    })
  })

  it('groups candidates and enrolls the picked ones', async () => {
    await groupFaces(['a', 'b'])
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/group', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ candidate_ids: ['a', 'b'] }),
    })
    await enrollFaces('Brian', ['a'])
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name: 'Brian', candidate_ids: ['a'], approved: true }),
    })
    await enrollFaces('Nanny', ['b'], false)
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name: 'Nanny', candidate_ids: ['b'], approved: false }),
    })
  })

  it('removes one photo and builds its thumbnail url', async () => {
    await deleteFacePhoto(3)
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/3', { method: 'DELETE' })
    expect(faceThumbUrl(3)).toBe('/api/ai/faces/thumbs/3')
  })

  it('updates a person by name, in the body', async () => {
    await updatePerson('Mom/Dad', { approved: false })
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces/people', {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name: 'Mom/Dad', approved: false }),
    })
    await updatePerson('Brain', { newName: 'Brian' })
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces/people', {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name: 'Brain', new_name: 'Brian' }),
    })
    await removePerson('Brian')
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces/people', {
      method: 'DELETE',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name: 'Brian' }),
    })
  })

  it('reads the bypass audit trail and files a report', async () => {
    await getFaceBypassStats()
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/bypass-stats', {})
    await getFaceRecognitionFeedback()
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/feedback', {})
    await submitFaceRecognitionFeedback('c1', 'false_negative')
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces/feedback/c1', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ report_type: 'false_negative', note: '', person_name: '' }),
    })
    await submitFaceRecognitionFeedback('c1', 'false_positive', 'not him', 'Brian')
    expect(fetch).toHaveBeenLastCalledWith('/api/ai/faces/feedback/c1', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ report_type: 'false_positive', note: 'not him', person_name: 'Brian' }),
    })
  })
})
