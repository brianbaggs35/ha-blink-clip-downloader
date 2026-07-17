import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './client'
import type {
  AiModelsResponse,
  AiStatus,
  AiUsage,
  AnalysisResultDict,
  CameraConfig,
  CheckpointsResponse,
  EscalationModelsResponse,
  FaceBypassStats,
  FaceFeedbackReportType,
  FaceRecognitionFeedback,
  FacesResponse,
  Feedback,
  FeedbackStats,
  FeedbackSubmission,
  FinetuneListResponse,
  MoondreamFinetune,
  MoondreamInstallStatusResponse,
  SuspiciousClip,
  TestEmailResult,
} from './types'

export function getAiStatus(): Promise<AiStatus> {
  return apiGet('/api/ai/status')
}

export function getAiUsage(): Promise<AiUsage> {
  return apiGet('/api/ai/usage')
}

export function clearAiUsage(): Promise<{ cleared: boolean }> {
  return apiDelete('/api/ai/usage')
}

export function fetchAiModels(): Promise<AiModelsResponse> {
  return apiGet('/api/ai/models')
}

export function getClipAiResult(clipId: string): Promise<AnalysisResultDict | null> {
  return apiGet(`/api/ai/results/${clipId}`)
}

export function getSuspiciousClips(limit = 20): Promise<SuspiciousClip[]> {
  return apiGet(`/api/ai/suspicious?limit=${limit}`)
}

export function analyzeClipNow(clipId: string): Promise<AnalysisResultDict> {
  return apiPost(`/api/ai/analyze/${clipId}`)
}

export function getMoondreamInstallStatus(): Promise<MoondreamInstallStatusResponse> {
  return apiGet('/api/ai/moondream/install-status')
}

export function startMoondreamInstall(): Promise<{ status: string; log?: string }> {
  return apiPost('/api/ai/moondream/install')
}

export function getCameraConfigs(): Promise<CameraConfig[]> {
  return apiGet('/api/ai/camera-configs')
}

export function saveCameraConfigs(configs: CameraConfig[]): Promise<{ saved: boolean; count: number }> {
  return apiPut('/api/ai/camera-configs', configs)
}

export function getFeedbackStats(camera?: string): Promise<FeedbackStats> {
  const query = camera ? `?camera=${encodeURIComponent(camera)}` : ''
  return apiGet(`/api/ai/feedback/stats${query}`)
}

export function getFeedbackForClip(clipId: string): Promise<Feedback | null> {
  return apiGet(`/api/ai/feedback/${clipId}`)
}

export function submitFeedback(
  clipId: string,
  body: FeedbackSubmission,
): Promise<{ saved: boolean } | { error: string }> {
  return apiPost(`/api/ai/feedback/${clipId}`, body)
}

export function deleteFeedback(clipId: string): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/feedback/${clipId}`)
}

export function getUntrainedFeedbackCount(): Promise<{ count: number }> {
  return apiGet('/api/ai/feedback/untrained-count')
}

export function listFaces(): Promise<FacesResponse> {
  return apiGet('/api/ai/faces')
}

export function enrollFace(
  name: string,
  imageBase64: string,
  approved = true,
): Promise<{ id: number; name: string; approved: boolean }> {
  return apiPost('/api/ai/faces', { name, image_base64: imageBase64, approved })
}

export function deleteFace(id: number): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/faces/${id}`)
}

export function setFaceApproved(id: number, approved: boolean): Promise<{ updated: boolean }> {
  return apiPatch(`/api/ai/faces/${id}`, { approved })
}

export function renameFace(id: number, name: string): Promise<{ updated: boolean }> {
  return apiPatch(`/api/ai/faces/${id}`, { name })
}

// Bulk-by-name variants — a multi-frame enrollment (ADVANCED FEATURE, see
// BiometricsPage.vue's "enroll from a clip" flow) stores one row per
// selected photo under the same name, so approving/renaming/removing a
// person should affect every one of their enrolled photos at once rather
// than one row at a time.
export function setFacesApprovedByName(name: string, approved: boolean): Promise<{ updated: boolean }> {
  return apiPatch(`/api/ai/faces/by-name/${encodeURIComponent(name)}`, { approved })
}

export function renameFacesByName(oldName: string, newName: string): Promise<{ updated: boolean }> {
  return apiPatch(`/api/ai/faces/by-name/${encodeURIComponent(oldName)}`, { name: newName })
}

export function deleteFacesByName(name: string): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/faces/by-name/${encodeURIComponent(name)}`)
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
): Promise<{ saved: boolean } | { error: string }> {
  return apiPost(`/api/ai/faces/feedback/${clipId}`, { report_type: reportType, note })
}

export function fetchEscalationModels(): Promise<EscalationModelsResponse> {
  return apiGet('/api/ai/models/escalation')
}

export function listFinetunes(): Promise<FinetuneListResponse> {
  return apiGet('/api/ai/finetune')
}

export function createFinetune(name: string, rank = 16): Promise<{ finetune_id: string }> {
  return apiPost('/api/ai/finetune', { name, rank })
}

export function getFinetune(finetuneId: string): Promise<MoondreamFinetune> {
  return apiGet(`/api/ai/finetune/${finetuneId}`)
}

export function deleteFinetune(finetuneId: string): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/finetune/${finetuneId}`)
}

export function listCheckpoints(finetuneId: string): Promise<CheckpointsResponse> {
  return apiGet(`/api/ai/finetune/${finetuneId}/checkpoints`)
}

export function activateCheckpoint(finetuneId: string, step: number): Promise<{ activated: boolean; model: string }> {
  return apiPost(`/api/ai/finetune/${finetuneId}/activate`, { step })
}

export function trainFromFeedback(
  finetuneId: string,
  limit = 10,
): Promise<{ trained: number; message?: string; finetune_id?: string; examples_attempted?: number }> {
  return apiPost(`/api/ai/finetune/${finetuneId}/train`, { limit })
}

export function saveCheckpoint(finetuneId: string): Promise<{ saved: boolean }> {
  return apiPost(`/api/ai/finetune/${finetuneId}/save-checkpoint`)
}

export function testEmail(): Promise<TestEmailResult> {
  return apiPost('/api/notifications/test-email')
}

export function testDiscord(): Promise<TestEmailResult> {
  return apiPost('/api/notifications/test-discord')
}

export function testMobile(): Promise<TestEmailResult> {
  return apiPost('/api/notifications/test-mobile')
}

export function testHaNotification(): Promise<TestEmailResult> {
  return apiPost('/api/notifications/test-ha')
}
