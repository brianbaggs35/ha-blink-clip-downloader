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
  gdrive_backed_up: boolean
  gdrive_file_id: string
  gdrive_uploaded_at: string
  notified: boolean
  face_recognized: boolean
}

export type ClipDetail = Omit<ClipListItem, 'notified' | 'face_recognized'>

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
  recognized_count: number
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
  /** False on a CPU that can't safely run PyTorch (e.g. Raspberry Pi 4's
   *  Cortex-A72) — gates the enhanced-detection pipeline and local face
   *  recognition. See vision.py's torch_cpu_compatible(). Undefined only
   *  when AI analysis itself is off (enabled: false). */
  torch_cpu_compatible?: boolean
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
  face_bypass_applied: boolean
  face_bypass_names: string
}

export type SuspiciousClip = AnalysisResultDict & {
  file_path: string
  clip_timestamp: string
  duration: number
  size_bytes: number
}

export type SuspiciousPeriod = 'today' | 'yesterday' | 'week' | 'month'

export interface SuspiciousClipsResponse {
  items: SuspiciousClip[]
  total: number
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

export interface CarZoneRect {
  shape: 'rect'
  x_min: number
  y_min: number
  x_max: number
  y_max: number
}

export interface CarZonePolygon {
  shape: 'polygon'
  points: [number, number][]
}

export type CarZone = CarZoneRect | CarZonePolygon

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
  // Whether this person counts toward the suspicious-flag bypass (see
  // analyzer.py's _face_bypass_applies) — a recognized-but-not-approved
  // enrollment is labeled but never suppresses an alert on its own.
  approved: boolean
}

export interface FacesResponse {
  available: boolean
  faces: FaceEnrollment[]
}

export interface FaceEnrollSuccess {
  id: number
  name: string
  approved: boolean
}

export interface FaceEnrollFailure {
  error: string
}

// The backend answers "no face detected" / "multiple faces detected" with
// HTTP 200 and this failure shape rather than a 400 — those are expected,
// recoverable outcomes of a normal enrollment attempt (a bad frame), not a
// malformed request, so callers must check for `error` rather than relying
// on the promise rejecting.
export type FaceEnrollResult = FaceEnrollSuccess | FaceEnrollFailure

export interface FaceBypassEvent {
  clip_id: string
  camera: string
  face_bypass_names: string
  analyzed_at: string
}

export interface FaceBypassStats {
  total_bypassed: number
  by_name: { name: string; count: number }[]
  recent: FaceBypassEvent[]
}

export type FaceFeedbackReportType = 'false_positive' | 'false_negative'

export interface FaceRecognitionFeedback {
  clip_id: string
  camera: string
  report_type: FaceFeedbackReportType
  note: string
  person_name: string
  created_at: string
}

// ---------------------------------------------------------------------
// Vehicle settings (Vehicles tab)
// ---------------------------------------------------------------------

export interface VehicleSettings {
  car_description: string
}

export interface EscalationModelsResponse {
  enabled: boolean
  models: AiModelEntry[]
  error?: string
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

// ---------------------------------------------------------------------
// Storage tab: Google Drive backup
// ---------------------------------------------------------------------

export type GDriveBackupPolicy = 'archived_only' | 'all_clips'

export interface GDriveSettings {
  client_id: string
  has_client_secret: boolean
  backup_policy: GDriveBackupPolicy
}

export interface GDriveStatus {
  configured: boolean
  connected: boolean
  account_email: string
  folder_id: string
  folder_name: string
}

export type GDriveConnectPhase = 'idle' | 'pending' | 'connected' | 'expired' | 'error'

export interface GDriveConnectState {
  phase: GDriveConnectPhase
  user_code?: string
  verification_url?: string
  expires_in?: number
  account_email?: string
  message?: string
}

export interface GDriveQuota {
  available: boolean
  limit?: number | null
  usage?: number
  usage_in_drive?: number
}

export interface GDriveQueueStatus {
  connected: boolean
  pending: number
  processing: number
  completed: number
  failed: number
}

export interface GDriveFolder {
  id: string
  name: string
  modified_time: string
}

export interface GDriveFoldersResponse {
  folders: GDriveFolder[]
}

// ---------------------------------------------------------------------------
// Live View
// ---------------------------------------------------------------------------

export type LiveViewState = 'starting' | 'live' | 'error'

export interface LiveViewStatus {
  active: boolean
  session_id?: string | null
  camera?: string | null
  state?: LiveViewState | null
  error?: string | null
}

export interface LiveViewCamerasResponse {
  cameras: string[]
}

// ---------------------------------------------------------------------------
// Security Feed — grid of near-live camera snapshot tiles
// ---------------------------------------------------------------------------

export interface SecurityFeedCamerasResponse {
  cameras: string[]
}

export interface SecurityFeedSettings {
  /** Which cameras to show as tiles. Empty means "show every camera" —
   * same convention as ai_car_cameras (see config.py). */
  cameras: string[]
  /** Tiles per row (1-3 — past 3, a tile shrinks too small to make out
   * what a snapshot actually shows). */
  columns: number
  /** How often the frontend re-fetches each tile's snapshot, in seconds
   * (5-300). This does not trigger a new Blink snapshot — it just re-reads
   * whatever the add-on's own poll cycle already cached; see
   * BlinkDownloader.get_camera_snapshot(). */
  refresh_seconds: number
}

// ---------------------------------------------------------------------------
// Storage — archived clip ZIP groups
// ---------------------------------------------------------------------------

export interface ArchiveGroup {
  archive_path: string
  clip_count: number
  total_size: number
  latest_timestamp: string
}

// ---------------------------------------------------------------------------
// Battery — per-camera state + history (Status tab)
// ---------------------------------------------------------------------------

/** battery_state is "ok"/"low" (Blink's own, reliable classification) or
 * possibly another raw value Blink reports — treat anything other than
 * "low" as normal rather than allow-listing just "ok". battery_level is a
 * coarse, non-percentage signal (not a true 0-100% charge) and
 * battery_voltage is hundredths of a volt (e.g. 165 = 1.65V) — both are
 * nullable since not every camera model reports them, and null is a real
 * "not reported" value, never coerced to 0. */
export interface BatteryStatus {
  camera: string
  battery_state: string
  battery_level: number | null
  battery_voltage: number | null
  recorded_at: string
}

export type BatteryHistoryEntry = BatteryStatus
