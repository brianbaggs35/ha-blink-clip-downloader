import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  activateCheckpoint,
  analyzeClipNow,
  clearAiUsage,
  createFinetune,
  deleteFace,
  deleteFeedback,
  deleteFinetune,
  enrollFace,
  fetchAiModels,
  getAiStatus,
  getAiUsage,
  getCameraConfigs,
  getClipAiResult,
  getFeedbackForClip,
  getFeedbackStats,
  getFinetune,
  getMoondreamInstallStatus,
  getSuspiciousClips,
  getUntrainedFeedbackCount,
  listCheckpoints,
  listFaces,
  listFinetunes,
  saveCameraConfigs,
  saveCheckpoint,
  startMoondreamInstall,
  submitFeedback,
  testEmail,
  trainFromFeedback,
} from './ai'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('ai api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getAiStatus() / getAiUsage() / clearAiUsage() / fetchAiModels()', async () => {
    await getAiStatus()
    expect(fetch).toHaveBeenCalledWith('/api/ai/status', {})
    await getAiUsage()
    expect(fetch).toHaveBeenCalledWith('/api/ai/usage', {})
    await clearAiUsage()
    expect(fetch).toHaveBeenCalledWith('/api/ai/usage', { method: 'DELETE' })
    await fetchAiModels()
    expect(fetch).toHaveBeenCalledWith('/api/ai/models', {})
  })

  it('getClipAiResult() / getSuspiciousClips() / analyzeClipNow()', async () => {
    await getClipAiResult('c1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/results/c1', {})
    await getSuspiciousClips()
    expect(fetch).toHaveBeenCalledWith('/api/ai/suspicious?limit=20', {})
    await getSuspiciousClips(5)
    expect(fetch).toHaveBeenCalledWith('/api/ai/suspicious?limit=5', {})
    await analyzeClipNow('c1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/analyze/c1', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('moondream install status/start', async () => {
    await getMoondreamInstallStatus()
    expect(fetch).toHaveBeenCalledWith('/api/ai/moondream/install-status', {})
    await startMoondreamInstall()
    expect(fetch).toHaveBeenCalledWith('/api/ai/moondream/install', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('camera configs get/save', async () => {
    await getCameraConfigs()
    expect(fetch).toHaveBeenCalledWith('/api/ai/camera-configs', {})
    const configs = [{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]
    await saveCameraConfigs(configs)
    expect(fetch).toHaveBeenCalledWith('/api/ai/camera-configs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configs),
    })
  })

  it('feedback stats/get/submit/delete/untrained-count', async () => {
    await getFeedbackStats()
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/stats', {})
    await getFeedbackStats('front')
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/stats?camera=front', {})
    await getFeedbackForClip('c1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/c1', {})
    await submitFeedback('c1', { correct: false, corrected_suspicious: true })
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/c1', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ correct: false, corrected_suspicious: true }),
    })
    await deleteFeedback('c1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/c1', { method: 'DELETE' })
    await getUntrainedFeedbackCount()
    expect(fetch).toHaveBeenCalledWith('/api/ai/feedback/untrained-count', {})
  })

  it('faces list/enroll/delete', async () => {
    await listFaces()
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces', {})
    await enrollFace('Alice', 'base64data')
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Alice', image_base64: 'base64data' }),
    })
    await deleteFace(3)
    expect(fetch).toHaveBeenCalledWith('/api/ai/faces/3', { method: 'DELETE' })
  })

  it('finetune list/create/get/delete/checkpoints/activate/train/save', async () => {
    await listFinetunes()
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune', {})
    await createFinetune('my-tune', 8)
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'my-tune', rank: 8 }),
    })
    await getFinetune('ft1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1', {})
    await deleteFinetune('ft1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1', { method: 'DELETE' })
    await listCheckpoints('ft1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1/checkpoints', {})
    await activateCheckpoint('ft1', 5)
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: 5 }),
    })
    await trainFromFeedback('ft1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: 10 }),
    })
    await saveCheckpoint('ft1')
    expect(fetch).toHaveBeenCalledWith('/api/ai/finetune/ft1/save-checkpoint', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('testEmail()', async () => {
    await testEmail()
    expect(fetch).toHaveBeenCalledWith('/api/notifications/test-email', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })
})
