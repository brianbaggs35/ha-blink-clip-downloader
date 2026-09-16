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

// One currently-failed analysis_queue row -- powers the AI tab's Queue
// Status "Failed" modal (AnalysisFailuresModal.vue). Mirrors
// GDriveFailedUpload below; clip_path is fetched but, like that type,
// deliberately not surfaced in the modal itself.
export interface AnalysisFailure {
  clip_id: string
  camera: string
  clip_path: string
  error_message: string
  completed_at: string
  retry_count: number
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
  detected_objects?: DetectedObjectSummary[]
  // Deterministic security assessment (see blink_downloader/security).
  // Zero/absent when the security layer produced nothing for this clip —
  // the optional object-detection pipeline is off, or nothing relevant was
  // detected.
  risk_score?: number
  severity?: SecuritySeverity
  event_type?: string
  evidence_quality?: number
  risk_override_applied?: boolean
  security_events?: SecurityEventRow[]
}

/** One detected box, normalized to 0-1 of the frame it came from so it
 *  overlays correctly at any player size. */
export interface DetectedBox {
  label: string
  confidence: number
  track_id: number | null
  offset_seconds: number
  box: [number, number, number, number]
}

export interface DetectedBoxesResponse {
  objects: DetectedBox[]
}

/** Ascending order — the same four bands the backend scores into. */
export type SecuritySeverity = 'routine' | 'noteworthy' | 'suspicious' | 'critical'

export const SECURITY_SEVERITIES: SecuritySeverity[] = ['routine', 'noteworthy', 'suspicious', 'critical']

/** One structured thing the deterministic layer believes happened.
 *
 * `confidence` is how sure the *detector* is given the evidence it had;
 * `evidence_quality` is how good that evidence was in the first place. They
 * are deliberately separate — see the backend's evidence.py. */
export interface SecurityEventRow {
  id: number
  clip_id: string
  camera: string
  event_type: string
  severity: SecuritySeverity
  confidence: number
  risk_score: number
  evidence_quality: number
  detail: string
  subject_label: string
  track_id: number | null
  asset_name: string
  asset_type: string
  start_offset: number
  end_offset: number
  evidence: Record<string, unknown>
  created_at: string
}

/** A timeline row: one clip, represented by its most severe event. */
export type SecurityTimelineRow = SecurityEventRow & {
  clip_timestamp: string
  file_path: string
  starred: boolean
  archived: boolean
  /** The AI model's own verdict on the same clip, from its most recent
   *  analysis — null when the clip has no analysis row at all. Shown beside
   *  the code-computed risk so agreement (and disagreement) between the two
   *  is visible without opening the clip. */
  ai_suspicious: boolean | null
  ai_confidence: number | null
  ai_summary: string | null
  risk_override_applied: boolean | null
}

export interface SecurityTimelineResponse {
  events: SecurityTimelineRow[]
  total: number
}

export interface SecurityStats {
  by_severity: Partial<Record<SecuritySeverity, number>>
  total: number
  days: number
}

/** What a camera has learned about where its protected vehicle sits. */
export interface VehicleSignatureInfo {
  camera: string
  learned: boolean
  established?: boolean
  sample_count: number
  box?: number[]
}

// One label's aggregate from the optional computer-vision object-detection
// stage (see vision.py's ObjectDetector, ai_enhanced_detection_enabled) —
// absent/empty unless that pipeline was on and found something.
export interface DetectedObjectSummary {
  label: string
  /** How many distinct ones appeared in the clip, by the tracker's own
   *  identities — not how many boxes were stored. The detector runs over
   *  every sampled frame, so one parked car contributes a box per frame;
   *  counting those is what reported three cars as "33". See database.py's
   *  own note for the accuracy this rests on. */
  count: number
  /** The raw box total behind that count, kept as supporting detail.
   *  Optional: a result stored by an older build has no such field. */
  detections?: number
  max_confidence: number
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

/** One weekly (``2026-W07``) or monthly (``2026-02``) usage bucket. Same
 *  shape as DailyUsageWire but keyed by a generic `period`, since the label
 *  differs per granularity. */
export interface PeriodUsageWire {
  period: string
  analyses: number
  tokens_prompt: number
  tokens_completion: number
  tokens_total: number
  cost: number | null
}

/** Weekly and monthly rollups, served from their own endpoint so the
 *  10s-polled /api/ai/usage payload never pays for a year-wide aggregate. */
export interface AiUsagePeriods {
  weekly: PeriodUsageWire[]
  monthly: PeriodUsageWire[]
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
  /** Missing on camera_configs.json entries created before v5.4.7; defaults to true. */
  auto_analyze?: boolean
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
  /** Uploads held independently of the connection — pausing used to mean
   *  disconnecting, which throws away the OAuth tokens and the chosen
   *  folder to achieve it. */
  uploads_paused: boolean
  /** Why, when the queue paused *itself* (a full Drive). Empty for a pause
   *  the user chose, which needs no explaining back to them. */
  pause_reason: string
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
  uploads_paused: boolean
  pause_reason: string
  /** Why the queue has stopped trying by itself (a full Drive, a rate
   *  limit), empty when nothing is holding it back. */
  hold_off_reason: string
  hold_off_seconds: number
  pending: number
  processing: number
  completed: number
  failed: number
}

export interface GDriveFailedUpload {
  clip_id: string
  camera: string
  clip_path: string
  error_message: string
  completed_at: string
}

/** One page of failed uploads plus how many there are in all — a spell of
 *  Drive being unreachable can fail every clip in the library, so the page
 *  on screen is not the size of the problem. */
export interface GDriveFailedUploadsPage {
  items: GDriveFailedUpload[]
  total: number
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
// Sync Module — arm/disarm the whole system or an individual camera
// ---------------------------------------------------------------------------

export interface SyncModuleCamera {
  name: string
  /** Per-camera arm state (blinkpy's motion_enabled) — whether this camera
   * records on motion while the system is armed. */
  armed: boolean
  online: boolean
  /** Normalized lowercase ("ok"/"low"), or null for a wired camera with no
   * battery at all (still shown — it can still be armed/disarmed). */
  battery_state: string | null
  battery_level: number | null
  wifi_strength: number | null
  /** blinkpy's product_type, e.g. "catalina" (Blink Outdoor), "owl" (Mini),
   * "lotus" (newer doorbell) — shown as a small badge, not otherwise used. */
  type: string | null
}

export interface SyncModuleInfo {
  name: string
  network_id: number | string | null
  serial: string | null
  /** Sync module firmware version, e.g. "2.13.30". */
  version: string | null
  status: string
  online: boolean
  /** Whole-system arm state for this sync module. */
  armed: boolean | null
  region_id: string | null
  local_storage: boolean
  cameras: SyncModuleCamera[]
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

export interface ArchiveClipsResponse {
  items: ClipListItem[]
  total: number
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
