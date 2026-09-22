import { INGRESS_ROOT } from '../env'
import { apiDelete, apiGet, apiPatch, apiPost } from './client'
import type {
  FaceBypassStats,
  FaceDetectResult,
  FaceEnrollResult,
  FaceFeedbackReportType,
  FaceGroupResult,
  FaceRecognitionFeedback,
  FaceScanResult,
  FacesResponse,
} from './types'

// The Biometrics tab's endpoints (media_server/faces.py). Enrolling is
// pick-from-what-was-found: a scan returns faces as thumbnails plus opaque
// candidate ids, and enrolling sends back only the ids picked — the
// embeddings behind them never reach the browser.

export function listFaces(): Promise<FacesResponse> {
  return apiGet('/api/ai/faces')
}

/** Every usable face in one clip, near-identical shots collapsed. Slow on
 * purpose-built hardware (seconds per clip on a Raspberry Pi), which is why
 * the picker scans one clip per request and shows progress between them. */
export function scanClipForFaces(clipId: string): Promise<FaceScanResult> {
  return apiGet(`/api/ai/faces/scan/${encodeURIComponent(clipId)}`)
}

export function detectFacesInPhoto(imageBase64: string): Promise<FaceDetectResult> {
  return apiPost('/api/ai/faces/detect', { image_base64: imageBase64 })
}

/** Group every candidate found so far by apparent person, across scans. */
export function groupFaces(candidateIds: string[]): Promise<FaceGroupResult> {
  return apiPost('/api/ai/faces/group', { candidate_ids: candidateIds })
}

/** `approved` only applies to a new person — photos added to someone
 * already enrolled take that person's current approval. */
export function enrollFaces(name: string, candidateIds: string[], approved = true): Promise<FaceEnrollResult> {
  return apiPost('/api/ai/faces', { name, candidate_ids: candidateIds, approved })
}

export function deleteFacePhoto(id: number): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/faces/${id}`)
}

export function faceThumbUrl(id: number): string {
  return `${INGRESS_ROOT}/api/ai/faces/thumbs/${id}`
}

// A person is every photo enrolled under one name. The name travels in the
// body, not the path: Home Assistant's ingress decodes a path before
// forwarding it, so a "/" in a name used to split it into two segments.
export function updatePerson(
  name: string,
  changes: { approved?: boolean; newName?: string },
): Promise<{ updated: boolean }> {
  const body: Record<string, unknown> = { name }
  if (changes.approved !== undefined) body.approved = changes.approved
  if (changes.newName !== undefined) body.new_name = changes.newName
  return apiPatch('/api/ai/faces/people', body)
}

export function removePerson(name: string): Promise<{ deleted: boolean }> {
  return apiDelete('/api/ai/faces/people', { name })
}

export function getFaceBypassStats(): Promise<FaceBypassStats> {
  return apiGet('/api/ai/faces/bypass-stats')
}

export function getFaceRecognitionFeedback(): Promise<FaceRecognitionFeedback[]> {
  return apiGet('/api/ai/faces/feedback')
}

export function submitFaceRecognitionFeedback(
  clipId: string,
  reportType: FaceFeedbackReportType,
  note = '',
  personName = '',
): Promise<{ saved: boolean } | { error: string }> {
  return apiPost(`/api/ai/faces/feedback/${clipId}`, { report_type: reportType, note, person_name: personName })
}
