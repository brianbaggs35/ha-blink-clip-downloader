import { apiGet, apiPost } from './client'
import { INGRESS_ROOT } from '../env'
import type { LiveViewCamerasResponse, LiveViewStatus } from './types'

export function getLiveViewCameras(): Promise<LiveViewCamerasResponse> {
  return apiGet('/api/liveview/cameras')
}

export function getLiveViewStatus(): Promise<LiveViewStatus> {
  return apiGet('/api/liveview/status')
}

export function startLiveView(camera: string): Promise<LiveViewStatus> {
  return apiPost('/api/liveview/start', { camera })
}

export function stopLiveView(sessionId?: string | null): Promise<{ stopped: boolean }> {
  return apiPost('/api/liveview/stop', sessionId ? { session_id: sessionId } : {})
}

export function sendLiveViewHeartbeat(sessionId: string): Promise<{ ok: boolean }> {
  return apiPost('/api/liveview/heartbeat', { session_id: sessionId })
}

export function liveViewPlaylistUrl(sessionId: string): string {
  return `${INGRESS_ROOT}/api/liveview/hls/${encodeURIComponent(sessionId)}/stream.m3u8`
}
