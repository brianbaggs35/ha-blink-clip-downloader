import { ApiError, apiDelete, apiGet, apiGetWithHeaders, apiPost, apiPut } from './client'
import type {
  AiModelsResponse,
  AiStatus,
  AiUsage,
  AiUsagePeriods,
  AnalysisFailure,
  AnalysisResultDict,
  CameraConfig,
  CheckpointsResponse,
  DetectedBoxesResponse,
  EscalationModelsResponse,
  Feedback,
  FeedbackStats,
  FeedbackSubmission,
  FinetuneListResponse,
  MoondreamFinetune,
  MoondreamInstallStatusResponse,
  SuspiciousClipsResponse,
  SuspiciousPeriod,
  TestEmailResult,
} from './types'

let cameraConfigUpdateQueue: Promise<void> = Promise.resolve()
export type CameraNameAliases = Record<string, string>

export function getAiStatus(): Promise<AiStatus> {
  return apiGet('/api/ai/status')
}

export function getAiUsage(): Promise<AiUsage> {
  return apiGet('/api/ai/usage')
}

export function getFailedAnalysisQueue(): Promise<AnalysisFailure[]> {
  return apiGet('/api/ai/queue/failed')
}

export function getAiUsagePeriods(): Promise<AiUsagePeriods> {
  return apiGet<AiUsagePeriods>('/api/ai/usage/periods')
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

export interface SuspiciousClipsParams {
  limit?: number
  offset?: number
  period?: SuspiciousPeriod
}

export function getSuspiciousClips(params: SuspiciousClipsParams = {}): Promise<SuspiciousClipsResponse> {
  const { limit = 20, offset = 0, period } = params
  const periodQuery = period ? `&period=${period}` : ''
  return apiGet(`/api/ai/suspicious?limit=${limit}&offset=${offset}${periodQuery}`)
}

export function getClipDetections(clipId: string): Promise<DetectedBoxesResponse> {
  return apiGet(`/api/ai/detections/${encodeURIComponent(clipId)}`)
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

async function getCameraConfigsSnapshot(): Promise<{
  configs: CameraConfig[]
  revision: string | null
  aliases: CameraNameAliases
}> {
  const response = await apiGetWithHeaders<CameraConfig[]>('/api/ai/camera-configs')
  const aliases = (() => {
    try {
      return JSON.parse(response.headers.get('X-Camera-Aliases') || '{}') as CameraNameAliases
    } catch {
      return {}
    }
  })()
  return { configs: response.data, revision: response.headers.get('ETag'), aliases }
}

export function saveCameraConfigs(
  configs: CameraConfig[],
  revision?: string,
): Promise<{ saved: boolean; count: number }> {
  return apiPut('/api/ai/camera-configs', configs, revision ? { 'If-Match': revision } : undefined)
}

export function updateCameraConfigs(
  buildConfigs: (latest: CameraConfig[], aliases: CameraNameAliases) => CameraConfig[],
): Promise<{ saved: boolean; count: number; configs: CameraConfig[] }> {
  const update = async (attempt: number): Promise<{ saved: boolean; count: number; configs: CameraConfig[] }> => {
    const snapshot = await getCameraConfigsSnapshot()
    const configs = buildConfigs(snapshot.configs, snapshot.aliases)
    try {
      const result = await saveCameraConfigs(configs, snapshot.revision ?? undefined)
      return { ...result, configs }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && attempt === 0) {
        return update(1)
      }

      throw error
    }
  }
  const operation = cameraConfigUpdateQueue.then(() => update(0))
  cameraConfigUpdateQueue = operation.then(
    () => undefined,
    () => undefined,
  )
  return operation
}

export function resolveCameraAlias(camera: string, aliases: CameraNameAliases): string {
  let current = camera
  const seen = new Set<string>()
  while (aliases[current.toLowerCase()] && !seen.has(current.toLowerCase())) {
    seen.add(current.toLowerCase())
    current = aliases[current.toLowerCase()]
  }
  return current
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
