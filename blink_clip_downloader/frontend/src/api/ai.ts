import { apiDelete, apiGet, apiPost, apiPut } from './client'
import type {
  AiModelsResponse,
  AiStatus,
  AiUsage,
  AnalysisResultDict,
  CameraConfig,
  CheckpointsResponse,
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

export function enrollFace(name: string, imageBase64: string): Promise<{ id: number; name: string }> {
  return apiPost('/api/ai/faces', { name, image_base64: imageBase64 })
}

export function deleteFace(id: number): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/ai/faces/${id}`)
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
