// Hand-written types mirroring the aiohttp backend's JSON payloads (see
// media_server.py's route handlers). No OpenAPI/pydantic schema exists to
// generate these from — aiohttp has no schema layer in this project — so
// this file is the single source of truth on the frontend side and must be
// kept in sync by hand as each tab is ported.

export type TwoFAState = 'connected' | 'needs_2fa' | 'error' | 'disconnected'

export interface AuthStatus {
  state: TwoFAState
  message?: string
  two_fa_result_seq?: number
  two_fa_result_ok?: boolean
}

// ---------------------------------------------------------------------
// Clips / cameras / library stats
// ---------------------------------------------------------------------

export interface ClipListItem {
  id: string
  camera: string
  file_path: string
  timestamp: string
  size_bytes: number
  duration: number
  source: string
  network_id: number
  starred: boolean
  tags: string[]
  downloaded_at: string
  archived: boolean
  archive_path: string
  notified: boolean
}

export type ClipDetail = Omit<ClipListItem, 'notified'>

export interface CameraStat {
  camera: string
  total: number
  size_bytes: number
  today: number
  this_week: number
  last_seen: string
}

export interface DiskStats {
  used_bytes: number
  used_mb: number
  free_bytes: number
  free_gb: number
  total_bytes: number
  total_gb: number
  quota_bytes: number
  quota_gb: number
}

export interface LibraryStats {
  total_count: number
  starred_count: number
  archived_count: number
  total_size_bytes: number
  today_count: number
  yesterday_count: number
  week_count: number
  disk?: DiskStats
  connected?: boolean
  account_id?: string
  last_download?: string
  next_poll?: string
  [key: string]: unknown
}

export interface ActivityRow {
  date: string
  hour: number
  count: number
}

// ---------------------------------------------------------------------
// AI status / queue / analysis
// ---------------------------------------------------------------------

export type AiProvider = 'ollama' | 'ollama_cloud' | 'moondream_cloud' | 'moondream_local' | 'anthropic' | 'openai'

export interface QueueStatus {
  schedule_start: string | null
  schedule_end: string | null
  in_schedule: boolean
  min_confidence: number
  pending: number
  processing: number
  completed: number
  failed: number
}

export interface AnalysisStats {
  total_analyzed: number
  suspicious_count: number
  total_frames_analyzed: number
  frames_analyzed_today: number
  last_analysis: string | null
}

export interface AiStatus {
  enabled: boolean
  prompt_debug_enabled: boolean
  ai_online?: boolean
  provider?: AiProvider
  model?: string
  car_protection_active?: boolean
  moondream_installed?: boolean
  moondream_arch_supported?: boolean
  escalation_provider?: AiProvider
  escalation_model?: string
  escalation_online?: boolean
  smtp_configured: boolean
  queue?: QueueStatus
  analysis_stats: AnalysisStats
}

export interface AnalysisResultDict {
  id?: number
  clip_id: string
  camera: string
  model: string
  response_text: string
  is_suspicious: boolean
  confidence: number
  summary: string
  frame_count: number
  analysis_duration: number
  analyzed_at: string
  tokens_prompt: number
  tokens_completion: number
  anomaly_score: number
  escalation_model: string
  escalation_tokens_prompt: number
  escalation_tokens_completion: number
  escalation_provider: string
  prompt_text?: string
}

export type SuspiciousClip = AnalysisResultDict & {
  file_path: string
  clip_timestamp: string
  duration: number
  size_bytes: number
}

export interface AiTestResult {
  success: true
  clip_id: string
  [key: string]: unknown
}

export interface ApiErrorBody {
  error: string
}

// ---------------------------------------------------------------------
// AI usage / cost tracking
// ---------------------------------------------------------------------

export interface TokenUsageModelRow {
  model: string
  provider?: string
  analyses: number
  tokens_prompt: number
  tokens_completion: number
  escalated: boolean
  cost: number | null
}

export interface DailyUsageWire {
  day: string
  analyses: number
  tokens_prompt: number
  tokens_completion: number
  tokens_total: number
  cost: number | null
}

export interface AiUsage {
  enabled: boolean
  provider?: AiProvider
  model?: string
  cost_per_1m_input?: number
  cost_per_1m_output?: number
  total_analyses: number
  total_tokens_prompt: number
  total_tokens_completion: number
  total_tokens: number
  total_escalations: number
  total_escalation_tokens: number
  by_model: TokenUsageModelRow[]
  total_estimated_cost: number | null
  daily: DailyUsageWire[]
}

// ---------------------------------------------------------------------
// Moondream local install
// ---------------------------------------------------------------------

export type MoondreamInstallStatus = 'idle' | 'installing' | 'installed' | 'failed' | 'unsupported'

export interface MoondreamInstallState {
  status: MoondreamInstallStatus
  log?: string
}

export interface MoondreamInstallStatusResponse {
  installed: boolean
  arch_supported: boolean
  install_state: MoondreamInstallState
}

// ---------------------------------------------------------------------
// AI models
// ---------------------------------------------------------------------

export interface AiModelEntry {
  name: string
  id?: string
  display_name?: string
  description?: string
  size?: number
  [key: string]: unknown
}

export interface AiModelsResponse {
  enabled: boolean
  models: AiModelEntry[]
}

// ---------------------------------------------------------------------
// Camera configs (AI tab)
// ---------------------------------------------------------------------

export interface CarZone {
  x_min: number
  y_min: number
  x_max: number
  y_max: number
}

export interface CameraConfig {
  camera: string
  description: string
  custom_prompt: string
  is_car_camera: boolean
  car_zone: CarZone | null
}

// ---------------------------------------------------------------------
// Feedback / adaptive learning
// ---------------------------------------------------------------------

export interface FeedbackStats {
  total: number
  correct: number
  incorrect: number
  false_positive: number
  false_negative: number
}

export interface Feedback {
  id: number
  clip_id: string
  camera: string
  analysis_result_id: number | null
  original_suspicious: boolean
  original_confidence: number
  correct: boolean
  correction_note: string
  corrected_suspicious: boolean | null
  created_at: string
  trained_at: string
}

export interface FeedbackSubmission {
  correct: boolean
  correction_note?: string
  corrected_suspicious?: boolean | null
}

// ---------------------------------------------------------------------
// Face recognition
// ---------------------------------------------------------------------

export interface FaceEnrollment {
  id: number
  name: string
  created_at: string
}

export interface FacesResponse {
  available: boolean
  faces: FaceEnrollment[]
}

// ---------------------------------------------------------------------
// Moondream Cloud fine-tuning
// ---------------------------------------------------------------------

export interface MoondreamFinetune {
  finetune_id?: string
  id?: string
  name?: string
  [key: string]: unknown
}

export interface FinetuneListResponse {
  enabled: boolean
  finetunes: MoondreamFinetune[]
}

export interface MoondreamCheckpoint {
  step: number
  [key: string]: unknown
}

export interface CheckpointsResponse {
  enabled: boolean
  checkpoints: MoondreamCheckpoint[]
}

// ---------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------

export interface TestEmailResult {
  success: boolean
  message: string
}
