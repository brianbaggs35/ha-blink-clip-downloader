import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getLiveViewCameras,
  getLiveViewStatus,
  liveViewPlaylistUrl,
  sendLiveViewHeartbeat,
  startLiveView,
  stopLiveView,
} from './liveview'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('liveview api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getLiveViewCameras()', async () => {
    await getLiveViewCameras()
    expect(fetch).toHaveBeenCalledWith('/api/liveview/cameras', {})
  })

  it('getLiveViewStatus()', async () => {
    await getLiveViewStatus()
    expect(fetch).toHaveBeenCalledWith('/api/liveview/status', {})
  })

  it('startLiveView()', async () => {
    await startLiveView('Front Door')
    expect(fetch).toHaveBeenCalledWith('/api/liveview/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera: 'Front Door' }),
    })
  })

  it('stopLiveView() with a session id sends it in the body', async () => {
    await stopLiveView('s1')
    expect(fetch).toHaveBeenCalledWith('/api/liveview/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: 's1' }),
    })
  })

  it('stopLiveView() with no session id sends an empty body', async () => {
    await stopLiveView()
    expect(fetch).toHaveBeenCalledWith('/api/liveview/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
  })

  it('stopLiveView(null) sends an empty body', async () => {
    await stopLiveView(null)
    expect(fetch).toHaveBeenCalledWith('/api/liveview/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
  })

  it('sendLiveViewHeartbeat()', async () => {
    await sendLiveViewHeartbeat('s1')
    expect(fetch).toHaveBeenCalledWith('/api/liveview/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: 's1' }),
    })
  })

  it('liveViewPlaylistUrl()', () => {
    expect(liveViewPlaylistUrl('s1')).toBe('/api/liveview/hls/s1/stream.m3u8')
  })

  it('liveViewPlaylistUrl() URL-encodes the session id', () => {
    expect(liveViewPlaylistUrl('a/b c')).toBe('/api/liveview/hls/a%2Fb%20c/stream.m3u8')
  })
})
