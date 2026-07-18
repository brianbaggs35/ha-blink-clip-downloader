# Changelog

## 5.0.4

### Bug fixes

- **Marking a clip's AI verdict Correct/Incorrect scrolled the Library tab
  back to the top**, even when the clip was far down a scrolled list.
  Feedback submission bumps a shared refresh signal so the AI tab's
  Adaptive Learning/Suspicious Activity views stay current — but the
  Library page's own reaction to that same signal unconditionally cleared
  its clip grid to empty before refetching, collapsing the page's
  scrollable height (and with it, the scroll position) an instant before
  repopulating it. That signal fires for reasons that never change which
  clips exist or how they're ordered, so this reload is now silent: it
  fetches in the background and swaps the list in place once ready,
  without ever clearing what's already on screen — the same reasoning
  already applied to the existing 60-second auto-refresh, just extended to
  this trigger too.
- **Hovering the clip player modal's previous/next buttons made them jump
  before settling into their slightly-larger hover state**, instead of
  just growing in place. Their `:hover` style carried a `translateY(-50%)`
  copied from the *wrapper* that actually needs it (an absolutely
  positioned element centering itself); the buttons themselves are plain
  flex children with no such offset to begin with, so hovering yanked each
  one up by half its own height on top of the intended scale. Removed the
  errant translate — hovering now only scales the button slightly, with no
  jump.

## 5.0.3

### Bug fixes

- **Fetching escalation (tier-2) models on the AI tab showed each model as
  raw JSON instead of its name.** The escalation picker's `models` field was
  typed as `string[]`, but the backend always returns objects
  (`{name, id, display_name, description}`) — so Vue's default text
  interpolation JSON-stringified the whole object into the dropdown. The
  escalation picker now renders exactly like the primary picker: just the
  model's plain id.
- **The OpenAI model list (both tiers) had no real organization.**
  Alphabetical sorting put the oldest, most expensive model (`gpt-4-turbo`)
  first purely because `-` sorts before `.`, dated snapshots
  (`gpt-4o-mini-2024-07-18`) were listed right alongside their bare alias,
  and a few non-vision variants (`gpt-4o-mini-transcribe`,
  `gpt-4o-mini-search-preview`) slipped through the vision filter. Every
  provider's model list (OpenAI, Anthropic, Ollama, Ollama Cloud, Moondream
  Cloud, Moondream Local) now returns exactly the bare id a user needs to
  paste into the Configuration tab — no pricing, no dates, no JSON — and
  OpenAI's list sorts newest-to-oldest instead of alphabetically.
- **The primary model picker's dropdown started out beside the "Fetch
  Models" button, then jumped underneath it the moment real models were
  fetched**, since the button and dropdown shared one wrapping row and the
  dropdown's width grew with its longest option. The dropdown and its Copy
  button now always sit on their own row below the fetch button, for both
  the primary and escalation pickers.
- **The primary and escalation model pickers had inconsistent layouts** —
  the escalation picker's provider/model summary and its controls were
  nested inside a shaded box the primary picker didn't have. Both now share
  the same structure (Provider/Model lines, then the fetch button, then the
  select+Copy row) and the same "best" convention: for OpenAI, `gpt-5.4-nano`
  is marked best for the primary model and `gpt-5.4-mini` for the
  escalation model, rather than whichever entry happens to sort first.

### Added

- **The Suspicious Activity Feed now paginates** instead of rendering every
  match at once. A Today/Yesterday/This week/This month filter (PrimeVue
  `Select`) narrows the results, and a PrimeVue `Paginator` (20 per page by
  default, with 10/20/50/100 options) replaces the old fixed 20-item cap.

## 5.0.2

### Bug fixes

- **The suspicious-feedback checkbox always proposed "should have been
  flagged suspicious," even for a clip that was already flagged
  suspicious.** Marking a suspicious clip's verdict incorrect and checking
  the corrected-verdict box submitted `corrected_suspicious: true` — i.e.
  "yes, still suspicious," which isn't a correction at all. The checkbox's
  label and the value it submits now flip with the clip's current verdict:
  "Should not have been flagged suspicious" when it currently is, "Should
  have been flagged suspicious instead" when it currently isn't.
- **"Today"/daily stats used the UTC calendar day instead of the
  household's actual timezone.** `get_stats()`, `get_camera_stats()`,
  `get_analysis_stats()`, `get_activity_data()`, and `get_daily_usage_stats()`
  all bucketed by comparing UTC-stored clip timestamps against a UTC
  calendar date — since the bundled PostgreSQL session is deliberately
  pinned to UTC, evenings in any timezone behind UTC (and mornings in any
  timezone ahead of it) got attributed to the wrong day. Most visibly, the
  Status tab's Activity chart and Clip Library "Today" count could roll
  over to "tomorrow" hours before local midnight. Both now resolve "today"
  against the system's actual configured timezone (the same one HA
  Supervisor gives the container), matching how clip timestamps already
  display correctly elsewhere in the UI.
- **Clicking "Send test notification" for the mobile app (or any of the
  other three test-notification buttons) surfaced a generic, unhelpful
  "check the add-on logs" error instead of the actual reason.** The
  `/api/notifications/test-*` endpoints returned HTTP 400 on a failed
  send, but the frontend's fetch wrapper throws on any non-2xx response
  before it ever reads the response body — so the specific message the
  backend went to the trouble of returning (e.g. "Mobile app target is not
  configured") never reached the toast. These are UI-only "try it and
  report the outcome" actions, not request-validation boundaries, so they
  now always return 200 and let the existing `success`/`message` body
  fields carry the result.
- **The Status tab's Blink Connection card truncated the Last download
  date/time with an ellipsis** even though there was room to wrap it onto
  a second line. Fixed for that row specifically, without changing the
  truncation behavior of other, shorter status values.
- **AI Usage tab stat tiles were in a confusing order** (Total Tokens
  ahead of the Prompt/Completion tokens that sum to it) **and left two
  tiles' worth of empty space** in the row Estimated Cost was stranded
  alone in. Reordered to a more sensible top row (Clips Analyzed / Total
  Tokens / Estimated Cost) and filled the previously-empty slots with two
  new derived stats — Avg. Cost / Clip and Models Used — computed from
  data the usage payload already includes, no new tracking added.
- **Several places still referenced raw config option keys** (e.g. a
  Biometrics tab banner reading "...set via `ai_face_recognition_enabled`
  in the add-on's Configuration tab...") **instead of the human-readable
  names those options actually show as in that tab.** Swept the Biometrics,
  Models, AI, and AI Usage tabs and replaced every such reference with its
  proper name from the add-on's own translations (e.g.
  `ai_face_recognition_enabled` → "Enable Local Face Recognition").

### Added

- **A clip's AI-verdict feedback (Correct/Incorrect) can now be cleared**,
  not just changed to the other verdict — a "Clear" button next to
  "Change" fully retracts it via the DELETE feedback endpoint that already
  existed but had no UI hooked up to it.
- **"Report a missed face match" now asks who was missed** when more than
  one person is enrolled (auto-attaching the name when there's exactly
  one), and "Wrong match" now records who the bad bypass actually matched.
  Stored alongside the existing report and shown on the Biometrics
  activity card — still a pure audit trail for a person to review and act
  on (e.g. re-enrolling someone with clearer photos), deliberately **not**
  fed into automatic face-match threshold adjustments, for the same safety
  reasons the suspicious-flag bypass itself is all-or-nothing (see
  `CLAUDE.md`/`database.py`'s `face_recognition_feedback` schema comment).
- **Tag entry now offers a typeahead dropdown of every tag already in
  use** instead of requiring the exact spelling to be retyped on every
  clip — click the "Add tag" box to see the full list, keep typing to
  filter it, and either pick a suggestion or finish typing a new tag. The
  list is the same one the Library filter dropdown already keeps fresh,
  so it stays current as tags are added or removed anywhere in the app.
- **The Models tab now links to a pricing page for each paid/cloud
  provider** (OpenAI, Anthropic, Moondream Cloud, Ollama Cloud) **and a
  documentation page for each free local one** (Ollama, Moondream Local),
  alongside each provider's existing reference link.

## 5.0.1

### Bug fixes

- **Pre-5.0.0 data (AI usage stats, starred/tagged/archived status on old
  clips, each clip's `source`, analysis history) never actually carried
  over the SQLite→PostgreSQL switch**, despite 5.0.0's own changelog entry
  claiming "no user-facing config or data-migration step" — that was only
  true of the *code* (nothing needed reconfiguring), not the *data*: an
  upgrading install started from a genuinely empty PostgreSQL database.
  Clips reappeared anyway purely as a side effect of an unrelated safety
  net (`library_scanner.py` reconstructing bare-bones rows — synthetic id,
  `source=''`, no starred/tags — from clip files still on disk), which
  masked the fact that everything that only ever lived in the database —
  most visibly, AI Usage tab stats, and every reconstructed clip's blank
  `source` — was gone. Nothing ever deletes the old SQLite file, so a new
  startup step (`sqlite_migration.py`) now imports
  `clips`/`analysis_results`/the usage reset marker from it, if present.
  Deliberately a *merge*, not a plain "only if the database is empty"
  import: anyone who already upgraded to 5.0.0 already has a non-empty,
  reconstructed `clips` table by the time this ships, so gating on
  emptiness would make the fix a permanent no-op for exactly the installs
  it exists for. Each old row is matched to an existing reconstructed one
  by file path and backfilled in place (camera, source, real timestamp,
  starred, tags, real download time, archived status) — preserving its
  current id, and with it any real analysis already run against it since
  upgrading — rather than assuming there's nothing there yet; a clip with
  no reconstructed match is inserted fresh under its original id. The
  whole operation runs in a single transaction, so a failure partway
  through is safe to simply retry on the next restart, and the old file is
  renamed on success so it never runs again once done. Deliberately
  doesn't import `face_enrollments`/`analysis_feedback`/per-camera
  baselines — biometrics is new in 5.0.0, so a pre-5.0.0 file never has
  enrollments to lose, and learned baselines are cheap to re-establish
  rather than risky to carry across a schema that changed meaningfully
  more between versions than clips/analysis_results did.
- **Biometrics enrollment frame picker sampled too sparsely to reliably catch
  someone facing the camera.** `GET /api/clips/{id}/frames` (used by the
  "enroll from a clip" flow) defaulted to a fixed 8 frames spread evenly
  across the whole clip regardless of length — a brief, low-motion moment of
  looking straight at the camera could easily land between samples. Now
  defaults to roughly one frame per second of the clip's actual duration
  (clamped to a max of 60 frames), with the enrollment picker paginating the
  results client-side (12 per page, selections preserved across pages)
  instead of rendering everything at once.
- **Vehicles tab: a `car_zone` saved before the persisted-snapshot picker
  redesign silently failed to render**, forcing a manual "Clear zone" +
  redraw on every camera that already had a zone configured pre-upgrade.
  Root cause: the picker's preview `<img>` requests
  `GET /api/vehicle/zone-snapshot/{camera}`, which 404s unless
  `/data/vehicle_zone_snapshots/<camera>.jpg` exists — but that file is
  only ever written by the zone-save endpoint, so any zone set before that
  endpoint existed has no snapshot. A 404'd `<img>` never fires `@load`, so
  the picker's container is never measured, and its saved-zone overlay
  silently never draws (both `previewRectStyle`/`previewPolygonAttr` bail
  out with no width/height) — leaving an apparently-blank picker with only
  "Clear zone" as a way forward. `_handle_vehicle_zone_snapshot_get` now
  falls back to that camera's newest clip thumbnail (matching what the
  picker always showed before the snapshot redesign) and persists it as
  the real snapshot, so this self-heals on first view with no data loss
  and no user action needed.
- **`openai_escalation_model` restored to `config.yaml`'s schema (marked
  deprecated)**, fixing a regression from 5.0.0: removing it from the
  schema entirely meant any install that had it set from before 4.0.0 now
  permanently logs `WARNING ... Option 'openai_escalation_model' does not
  exist in the schema` from the Supervisor on every options validation
  pass, since the value was still present in that install's saved options
  with no matching schema entry to validate it against. No behavior
  change — it was, and still is, read and auto-migrated to
  `ai_escalation_enabled`/`ai_escalation_provider`/`ai_escalation_model` on
  startup (see `config.py`'s `_resolve_ai_escalation()`); this only stops
  the spurious warning. See `DOCS.md`'s AI Provider Settings section.

## 5.0.0

Major release, bumped from 4.1.0 given the scope of what landed together: a
complete visual redesign of the web UI, an optional off-by-default
computer-vision enhancement pipeline layered on top of the existing
AI-provider prompt pipeline, the Moondream Cloud fine-tuning panel wired
end-to-end to human feedback, and dedicated Vehicles/Biometrics tabs with a
face-recognition suspicious-flag bypass. None of the AI/CV work changes
default behavior — every new stage and feature is disabled or empty out of
the box and the add-on analyzes clips exactly as it did in 4.0.2 until
explicitly turned on.

> ⏱️ **Updating from 4.0.2 or earlier takes noticeably longer than a normal
> add-on update — expect roughly 10-15 minutes, not the usual under-a-minute
> restart.** This release also carries 4.1.0's base-image switch (see
> "Breaking change — Docker base image" below): 4.0.2 and earlier ran on
> Alpine, this release runs on Debian, and those two share no image layers
> at all, so the Supervisor has to pull a genuinely new image from scratch
> rather than an incremental diff — compounded by the new bundled
> PostgreSQL server and (unconditionally installed, even if left disabled)
> computer-vision pipeline dependencies pushing the image itself to around
> 4.2 GB (see the root README's System Requirements table). This is a
> one-time cost of crossing that boundary, not a sign anything is stuck —
> give it time before assuming the update has hung.

### Added — Vehicles tab

- New **Vehicles** nav tab, the dedicated home for protected-vehicle
  monitoring settings (previously split between the add-on's Configuration
  tab and a car-zone editor buried in the AI tab's Camera Configurations
  section). Lets you set the **Protected Vehicle Description** directly from
  the web UI for the first time (previously only settable via the HA
  Supervisor's Configuration tab), and mark which camera(s) can see the
  vehicle.
- **Visual car-zone picker**: replaces the old four-number percentage-entry
  fields with a real click-drag rectangle selector drawn over an actual
  camera frame — draw, move, and resize (via corner handles) a zone marking
  exactly where your vehicle sits. Browse the last several recent clips from
  that camera to pick a frame that actually shows the vehicle clearly. This
  is what lets you accurately mark *your* car when several vehicles are
  parked close together (shared/apartment parking). A "Clear zone" action
  removes it entirely. Stored in the exact same `car_zone` shape the backend
  already validated, so no data migration was needed.
- Zone picker: a **"Clear" button** resets a bad in-progress rectangle or
  freeform trace back to nothing (without discarding the whole edit or
  falling back to a previously-saved zone), so a mis-drawn shape can be
  wiped and immediately redrawn on the same frame.
- `CameraConfigsSection` (AI tab) now only owns camera description/custom
  prompt — the car-camera checkbox and zone editor moved to the Vehicles tab.

### Added — Biometrics tab and face-recognition suspicious-flag bypass

- New **Biometrics** nav tab (replacing the AI tab's Face Recognition
  Enrollment section) for enrolling household members' faces. An approved,
  recognized person can now automatically clear a clip's suspicious flag —
  **only when every other face in the same clip also belongs to an approved
  enrollment**; a single stranger, or a recognized-but-not-approved person,
  standing next to an approved family member still gets flagged normally.
  This directly targets false positives on cameras that watch the same few
  people every day (e.g. a front door), without weakening detection of
  anyone actually unrecognized.
- **Multi-frame enrollment from a clip** (recommended enrollment method): pick
  a camera and a recent clip, extract several frames from it, and select as
  many as you like that show the face clearly — across whatever angles/
  lighting a real motion-triggered clip actually produced, rather than a
  single posed photo. Each selected frame is stored as its own enrollment
  under the same name, so recognition has multiple reference angles to match
  against. The simple single-photo upload flow is still available as an
  alternative.
- **Per-person approval**: enrolling ≠ automatically trusting forever — each
  enrolled person has an `approved` flag (defaults on) controlling whether
  they count toward the bypass; flip it off to keep recognizing/labeling
  someone (e.g. a regular visitor) without granting them bypass trust. New
  bulk by-name endpoints (`PATCH`/`DELETE /api/ai/faces/by-name/{name}`) let
  the UI manage every photo enrolled for one person as a single unit
  (approve/rename/remove all at once) instead of one row at a time.
- **Notification personalization**: when the bypass fires, the human-facing
  summary is rewritten locally to name the recognized person (e.g. "Brian
  walked up the driveway" instead of "A person walked up the driveway") —
  computed entirely after the AI's response has already returned, using only
  locally-known names. Never applied when a stranger/unapproved person is
  also present.
- **Privacy guarantee, enforced not just documented**: the prompt text sent
  to any AI provider (including cloud providers) now only ever contains a
  name-free count/fact ("N locally-enrolled household member(s) matched") —
  never a name, photo, or embedding, regardless of provider. Names are only
  ever used afterward, locally, for the notification personalization above.
  The Biometrics tab states this guarantee explicitly, and it's covered by a
  dedicated prompt-leakage test.
- New DB migration (the first since the SQLite→PostgreSQL switch): adds
  `face_enrollments.approved` via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
  applied automatically on startup for existing installs.

### Fixed

- **Protected Vehicle Description didn't survive a restart**: setting it
  purely from the Vehicles tab (`vehicle_settings.json`) took effect
  immediately in the running process via `update_car_description()`, but
  startup only ever read `ai_car_description` from `options.json` — an
  add-on restart or update would silently revert to whatever (usually
  empty) value was last set in the Supervisor's Configuration tab. Startup
  now reads `vehicle_settings.json` first, matching the same "web UI file
  is authoritative once written, falls back to the config.yaml option only
  until then" precedent `camera_configs.json` already followed.
- **Zone-motion prompt leakage**: `_maybe_compute_zone_motion` was the one
  car-zone code path that didn't check whether protected-vehicle rules
  actually apply (i.e. whether a Protected Vehicle Description is set) before
  computing and injecting a `ZONE MOTION: ... near the protected vehicle's
  usual spot` prompt segment — every other car-zone-aware code path already
  gated on this correctly. A camera marked as seeing the vehicle with a zone
  configured, but no description set yet, could get this misleading text
  injected into its prompt. Now gated identically to every other car-zone
  path.
- **Escalation model picker**: the AI tab's "fetch models" dropdown only ever
  applied to the tier-1 model. Added the same fetch/select/copy affordance
  for the tier-2 escalation model (`GET /api/ai/models/escalation`).
- **Deprecated `openai_escalation_model` removed** from `config.yaml`'s
  add-on options schema (still fully migrated on startup for existing
  installs via `ai_escalation_provider`/`ai_escalation_model` — no
  behavior change for anyone with it already set).
- **Clip duration** now shown as text in the Library list's clip-meta row
  (camera/date/size line), not just as a small overlay badge on the
  thumbnail.
- Fixed a duplicate-fetch bug in the new clip-frame-selection UI: selecting
  a camera fetched its recent clips twice due to two watchers both firing on
  the initial selection.
- **Tier-2 escalation lost camera identity on protected-vehicle cameras**:
  `_maybe_escalate` invokes the tier-2 analyzer directly rather than through
  its own `_analyze_clip_locked()`, so the escalation analyzer's
  `_current_camera` was never set — Moondream Cloud/Local's car-protection
  distance rules and zone lookup silently evaluated for the wrong (empty)
  camera on every escalated call whenever `ai_car_cameras` was a non-empty,
  restricted subset. Masked when `ai_car_cameras` is empty (the default),
  which is why this went unnoticed. `_maybe_escalate` now propagates the
  camera explicitly before invoking tier 2.
- **JSON boolean coercion false positive**: a `"suspicious": "false"` JSON
  *string* response (plausible from smaller/looser vision models) was
  coerced with `bool(...)`, and `bool("false")` is `True` in Python —
  flipping a model's clearly-intended "not suspicious" verdict into a
  spurious suspicious flag. Only the literal string `"true"`
  (case-insensitive) now counts as suspicious.
- **Camera-configs save could silently wipe every camera's settings**: the
  `PUT /api/ai/camera-configs` handler had no `isinstance(body, list)`
  check, so a syntactically-valid-but-wrong-shaped body (e.g. `{}`) iterated
  to zero entries with no error and overwrote `camera_configs.json` with an
  empty array. Now validated and rejected with 400.
- **Per-camera stats didn't merge case-insensitively**: `get_camera_stats`'s
  `GROUP BY LOWER(camera), camera` defeated its own case-folding by
  including the raw column, unlike every other camera-matching query in the
  file — a renamed/retyped camera with different casing produced two
  separate stat rows instead of one merged row.
- **A failed alert dispatch could mark a successful analysis as "failed"
  and silently drop the alert**: the analysis queue wrapped
  `analyze_clip()`+`add_analysis_result()`+`update_queue_status("completed")`
  *and* the alert dispatch in one try/except, so a transient failure in the
  dispatch step (e.g. the adaptive-threshold DB lookup) overwrote an
  already-successful, already-persisted analysis's status with `"failed"` —
  triggering a wasted re-analysis and losing the alert for a genuinely
  suspicious clip. Dispatch failures are now caught and logged separately
  without touching the analysis's own status.
- **Archiver could crash a whole month's batch (and duplicate clips into the
  zip on retry) on a single DB error**: `mark_archived` failures weren't
  caught (only zip/OS errors were), so one clip's DB error aborted every
  remaining clip in the run. Each step is now isolated so one clip's failure
  doesn't affect the rest of the batch.
- **Contact-analysis proximity numbers overstated by ~2.3x**: the CONTACT
  ANALYSIS prompt hint's pixel-gap estimate used the segmentation mask's
  full dilation-kernel size instead of its radius. Only affected the
  descriptive number shown to the AI provider, not the touching/not-touching
  verdict itself.
- **CLAHE-enhanced frames unintentionally fed face recognition**: with both
  `enhanced_detection_enabled` and `face_recognition_enabled` on, face
  embeddings were computed from contrast-enhanced/denoised frames while
  enrolled reference embeddings come from raw photos — an embedding-space
  mismatch that could cause an approved household member to go
  unrecognized. Face recognition now always runs against the original raw
  frames.
- **Mismatched-length face embeddings were silently truncated and
  compared**: `cosine_similarity` used `zip()`, which truncates to the
  shorter vector instead of rejecting a dimensionality mismatch (e.g. after
  an embedding-model change or a corrupted DB row) — now returns `0.0`
  ("can't compare, no match") instead.
- **A killed process mid-download could permanently corrupt a clip file**:
  clips were streamed directly to their final destination path; a container
  restart or OOM-kill mid-write left a truncated file that the
  already-downloaded check treated as complete forever. Downloads now
  stream to a temp file and atomically rename over the destination only
  after a full, successful write, matching the pattern already used for the
  auth-token file.
- Several smaller robustness fixes found in the same review: `/data/stats.json`
  and the digest's last-sent state file are now written atomically (temp
  file + rename) like the auth-token/tracker files already were;
  `retry_delay` now has an upper bound like every sibling numeric option;
  `PUT /api/clips/{id}/star` now rejects malformed JSON with 400 instead of
  silently defaulting to `starred: true`, matching its `/tags` sibling;
  Blink API clip-list pagination now has a defensive page cap; a swallowed
  per-frame exception in the CV preprocessing stage is now logged at debug
  level; and two stats queries were parameterized instead of interpolating
  date strings directly into SQL (no injection risk since the values were
  already server-computed, just an inconsistent pattern).
- **Library "Yesterday" filter actually meant "yesterday onward"**: the
  date-range filter only set a lower bound, so selecting "Yesterday"
  returned everything from yesterday-midnight onward, including today.
  Now bounds both ends of the day.
- **Moondream install-progress polling leaked past tab switches**: the AI
  tab is destroyed on tab switch, but its install-status polling
  `setTimeout` loop had no unmount cleanup and kept re-fetching in the
  background forever if a user switched away mid-install.
- **Stale network responses could overwrite newer ones in the Library
  grid**: switching camera, editing the search box, and the periodic
  refresh could all fire overlapping clip-list requests with no ordering
  guarantee — whichever happened to resolve last won, even if it was no
  longer the active filter selection. Responses are now discarded unless
  they're still the most recently fired request. The same fix was applied
  to the Vehicles zone-picker and the Biometrics enrollment picker's
  clip/frame loading, which had the same gap.
- **AI tab's Adaptive Learning stats and Suspicious Activity Feed never
  refreshed** after submitting feedback from a clip opened via the feed
  (reachable from the AI tab without switching tabs) — both child
  components already exposed a `reload()` for exactly this, but nothing
  ever called it.
- **Global `?`/Esc shortcuts fired while typing**: the keydown handler only
  excluded `<input>`, not `<textarea>`/`<select>`/content-editable elements
  — typing a literal `?` in, e.g., the Vehicles tab's description field
  toggled the help overlay mid-keystroke.
- **`IconName` had silently collapsed to plain `string`**: an explicit
  `Record<string, IconDef>` annotation on the icon lookup table defeated
  `keyof typeof ICONS`'s literal-union inference, so a typo'd icon name
  would have compiled cleanly and crashed at render instead of being caught
  by the type checker. Restored via `satisfies` instead of a type
  annotation.
- Minor clip-modal fixes: Escape now blurs a focused tag/note input instead
  of doing nothing; a denied fullscreen request now shows a toast instead of
  an unhandled promise rejection; and a dead prop watcher (unreachable since
  the modal already fully remounts the AI panel on clip change) was removed.

A subsequent final line-by-line pass over every backend and frontend file
turned up a further batch, mostly the same bug classes recurring in code
paths the first review didn't examine:

- **Local-storage clip downloads had the same non-atomic-write bug already
  fixed for cloud-clip downloads**: `_download_local_storage_item()` passed
  the destination path straight to blinkpy's `download_video()`, which opens
  it in truncate mode and writes the whole response in one call — a process
  kill mid-download left a truncated file that the already-downloaded check
  then treated as complete forever. Now downloads to a temp path and
  atomically renames over the destination, matching every other download
  path.
- **A clip's AI panel could get permanently stuck on "Failed to load
  analysis"**: a failed (re-)analyze attempt set an error flag that nothing
  ever cleared, and the panel's own reload guard prevented it from ever
  fetching again — hiding a still-valid previous result behind a dead end
  with no retry button until the whole panel remounted. A failed attempt now
  just toasts the error and leaves whatever was already showing in place.
- **The clip modal had the same stale-response-race bug already fixed in the
  Library grid and the Vehicles/Biometrics pickers, in a third spot**: rapid
  prev/next navigation could let an abandoned clip's slow response land
  after a newer one and overwrite the player and metadata back to the wrong
  clip. Same request-sequencing fix applied.
- **The Moondream local-install flow had two ways to get stuck**: a failed
  install request left the panel showing "Installing…" forever with no
  button (nothing ever rolled the optimistic state back), and a single
  dropped status-poll request (plausible over the several minutes an install
  can take) silently killed the whole progress tracker even though the
  install was likely still proceeding fine in the background. Both now
  recover: a failed start reverts to the retry button, and polling keeps
  going regardless of one attempt's outcome.
- **`fast_poll_duration` had no upper bound**, unlike its sibling options and
  its own `config.yaml` schema (which already declared one) — a typo'd huge
  value left the add-on polling Blink at the aggressive fast-poll rate
  indefinitely after a single motion event.
- A photo selected for Biometrics enrollment left its preview's blob URL
  un-revoked if the tab was switched away from before enrolling or clicking
  Clear (the tab is destroyed on switch); a dropped anomaly-score lookup and
  two state-file loaders (clip tracker, digest) failing on an unreadable
  (not just corrupt) file were logged/handled the same way their siblings
  already were, instead of staying silent or crashing the add-on at startup.

### Added — Automations tab

- New **Notification Channels** panel: one-off test actions for Discord and
  mobile-app push notifications (mirroring the existing "Send Test Email"),
  so all three channels can be verified from the web UI before enabling them
  for real alerts.

### Housekeeping

- Removed the ~2,900-line dead `_HTML` string in `media_server.py` — a
  leftover of the pre-Vue embedded single-file UI, fully superseded by the
  Vue frontend and confirmed unreferenced anywhere.

### Redesigned — web UI visual overhaul

- Replaced the web UI's entire visual design system (`media_server.py`'s
  embedded `<style>` block): a refined "modern SaaS dashboard" look
  (near-black dark theme with an indigo accent, a matching crisp light
  theme) replaces the earlier GitHub-dark-clone palette, with a tiered
  radius/shadow/spacing scale applied consistently across every card,
  button, input, badge, modal, and table instead of the previous ad-hoc
  per-rule values. Added refined hover/focus states (including
  `:focus-visible` rings for keyboard accessibility), subtle custom
  scrollbars, and softer motion (spring-like easing on hovers, card lifts,
  modal transitions).
- Replaced emoji icons in the app's primary chrome — the sidebar brand
  mark, all five nav tabs, the theme/notifications toggle buttons, and the
  clip grid's empty-state/no-thumbnail placeholders — with a small
  hand-authored inline SVG icon set (`.icon`, stroke-based, sized off
  `currentColor` so it themes automatically with dark/light and hover
  states). This was the most dated-feeling part of the previous UI:
  full-color platform emoji rendered inconsistently across operating
  systems and looked out of place against the new monochrome design
  language. Contextual/decorative emoji deeper in dynamically-generated
  content (feedback thumbs-up/down, toast confirmations, table icons) were
  intentionally left as-is — low risk, low visual impact, and in several
  cases (👍/👎 feedback) already the clearest possible affordance.
- No functionality changed: every `id` attribute and JS-referenced class
  name the script depends on (`$('...')` lookups, `classList` toggles) was
  preserved exactly, since the SPA has no data-binding framework and is
  tightly coupled to those hooks. Verified with the existing Playwright e2e
  smoke check (`e2e/smoke.mjs`) plus manual screenshots across both themes
  and every tab (library, status, AI usage, automations, AI) — video
  playback, downloads, starring/tagging/deleting clips, bulk selection,
  the AI analysis panel and its feedback controls, camera configuration,
  and face-recognition enrollment all still work unchanged.
- Considered and rejected loading a webfont (Inter) for the "premium SaaS"
  feel — it would have required loosening the media server's
  Content-Security-Policy (`style-src`/`font-src` currently only allow
  same-origin, inline, and `cdn.jsdelivr.net` for Video.js) to also permit
  Google Fonts, and would add an external network dependency for text
  rendering that fails ungracefully on a Home Assistant instance without
  outbound internet. Stuck with a refined system-font stack
  (`-apple-system`/`Segoe UI`/`Roboto`/etc.) instead, which already renders
  well on every real target platform and requires no CSP change.

### Breaking change — SQLite replaced with a bundled PostgreSQL 17 server

- The clip library (`database.py`) now runs against a real PostgreSQL 17
  server, bundled and supervised inside this same add-on container (see the
  `postgresql-17` package install in `Dockerfile`, the one-shot data
  directory bootstrap in `rootfs/etc/cont-init.d/01-postgres-init.sh`, and
  the supervised server process in `rootfs/etc/services.d/postgresql`),
  replacing the previous single-file `aiosqlite` database. The Postgres data
  directory lives under `/data/postgresql/17/main` so it survives add-on
  updates/restarts the same way the old SQLite file did; the `blink-downloader`
  service now waits on `pg_isready` before starting so the first query of a
  fresh container start never races the server finishing recovery. Connects
  over a local Unix domain socket with no password — trust auth is scoped to
  that socket only, never exposed outside the container, matching the same
  trust boundary the previous SQLite file relied on (filesystem permissions
  alone), not a weaker one. No user-facing config or data-migration step:
  this is entirely an internal storage-engine swap behind the same
  `enable_library_db` option, and gains real concurrent access, native
  boolean/float/identity types, and richer query support (window functions,
  `RETURNING`, etc.) over what SQLite offered.

### New feature — local standalone testing without Home Assistant OS

- Added `local-test/run.sh` and `local-test/options.json.example`: running
  the built image directly with `docker run` previously failed immediately
  with `FileNotFoundError: Options file not found: /data/options.json`,
  since the HA Supervisor (not present outside of real HA OS) is what
  normally writes that file and bind-mounts `/data`/`/share` before
  starting the container. The new script builds the image and runs it with
  local `data`/`share` directories mounted the same way, prompting on first
  run to fill in real Blink credentials. Documented in `CONTRIBUTING.md`.
  Ingress and Supervisor/Core-API-dependent features (HA notifications,
  `watch_ha_events`) aren't available this way, but everything else
  (polling, downloads, the web UI, AI analysis) works identically — this is
  a fast inner-dev-loop check, not a substitute for a real HA OS VM before
  opening a PR.

### New feature — Moondream fine-tuning, wired to feedback end-to-end

- The AI tab's Fine-Tuning panel could already create a fine-tune, list
  checkpoints, and activate one for live inference — but the underlying
  rollout/training API (`MoondreamFineTuneManager.generate_rollouts()` /
  `.train_step()` / `.save_checkpoint()`) was fully implemented and tested
  yet unreachable from any HTTP route or UI control, and `DOCS.md` claimed
  the panel let you "create a fine-tune from your own labeled examples,"
  which wasn't actually true. Added a **Train from Feedback** button: for
  each 👍/👎 clip-analysis correction not yet used for training, it
  re-extracts a representative frame from that clip, pairs it with the
  camera's analysis prompt and the corrected (or, for confirming 👍
  feedback, original) suspicious/not-suspicious verdict, and runs one
  supervised fine-tuning step per example. A new **Save Checkpoint** button
  persists the trained result so it shows up under Checkpoints to activate.
  Feedback rows are marked consumed once trained so repeated runs only pick
  up what's new. `ClipDatabase` gained `get_untrained_feedback()` /
  `mark_feedback_trained()` and an `analysis_feedback.trained_at` column to
  track this.

### Bug fix — face recognition could fail an entire clip's analysis

- Every other computer-vision stage (frame preprocessing, object detection,
  depth estimation, contact segmentation) degrades to "no hint" on internal
  failure, per `vision.py`'s documented graceful-degradation contract — but
  `FaceRecognizer.recognize()` had no guard around its database lookup or
  embedding parsing. A DB error or corrupted embedding row there would have
  propagated uncaught out of `VisionPipeline.process_clip()` and failed the
  *entire clip's* analysis (marked `failed` in the queue) instead of just
  skipping the RECOGNIZED RESIDENT hint like every other stage would. Now
  wrapped in the same try/except-and-log pattern as its siblings.

### New feature — computer-vision enhancement pipeline

- Added `vision.py`: five independently-toggleable stages, each lazily
  importing its own heavy dependency and reporting itself unavailable
  rather than raising if that dependency isn't installed:
  - **Frame preprocessing** (`ai_cv_preprocessing_enabled`) — CLAHE contrast
    enhancement + light denoising (OpenCV) applied to frames before they're
    sent to the AI model.
  - **Object detection + tracking** (`ai_object_detection_enabled`,
    `ai_object_detection_model`) — YOLO (Ultralytics) + ByteTrack, feeding a
    code-computed OBJECT DETECTION hint (detected classes, and pixel-based
    person-to-vehicle distance) into the prompt, more precise than
    motion-diff heuristics alone.
  - **Depth estimation** (`ai_depth_estimation_enabled`) — Depth Anything V2
    (via transformers), distinguishing "overlapping in the 2D frame" from
    "actually at the same distance from the camera" for a detected
    person/vehicle pair. Requires object detection.
  - **Contact segmentation** (`ai_segmentation_enabled`) — SAM2 (via
    transformers), refining a bounding-box overlap into an actual
    touching-or-not judgment using each object's real segmented outline.
    The heaviest stage; requires object detection.
  - **Local-only face recognition** (`ai_face_recognition_enabled`) —
    facenet-pytorch (MTCNN + InceptionResnetV1). Household members are
    enrolled from a single reference photo via the AI tab's new **Face
    Recognition Enrollment** panel; photos and embeddings are stored only
    in this add-on's own database and never uploaded to any cloud AI
    provider, regardless of `ai_provider`. A match adds a RECOGNIZED
    RESIDENT hint to the prompt.
  - Every hint is appended to the same prompt the SCENE BASELINE/ZONE
    MOTION hints already use — the configured AI provider still makes
    every suspicious/not-suspicious call; these stages only improve the
    evidence it reasons over.
- All five options are off by default and require substantial additional
  CPU/RAM plus first-use model downloads (100MB-800MB+ each, cached under
  `/data`) when enabled — see the new "Computer-Vision Enhancement
  Pipeline" section in DOCS.md for hardware guidance. Not recommended on a
  Raspberry Pi or similarly constrained device.

### Breaking change — Docker base image

- Switched the add-on's base image from
  `ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.20` (Alpine/musl)
  to `ghcr.io/home-assistant/{arch}-base-debian:trixie` (Debian/glibc).
  PyTorch — a dependency of the object-detection, depth-estimation,
  contact-segmentation, and face-recognition stages above — has no
  official wheels for musl/Alpine on any architecture (the same class of
  problem already documented for moondream's TVM build on aarch64
  musllinux), so there was no way to support this pipeline on the previous
  base image. Trixie ships Python 3.13 by default, satisfying this
  project's `requires-python = ">=3.12"`. This grows the baseline image
  size for every install by tens of MB even with the entire CV pipeline
  left disabled, since it's a different base OS, not an optional layer.

### Bug fixes — camera isolation and animal-vehicle contact

- **Fix: vehicle-proximity/depth/contact hints were not actually isolated
  per camera.** `VisionPipeline.process_clip()` only checked whether a
  protected-vehicle description existed *anywhere* in config, not whether
  *this specific camera* is one of the cameras designated to view it (the
  same distinction `ai_car_cameras`/`_car_protection_applies` already
  enforces for the base prompt). A camera outside `ai_car_cameras` that
  happened to detect an unrelated car and an unrelated person — a front
  door camera catching a passing vehicle, say — could have generated an
  OBJECT DETECTION/DEPTH/CONTACT hint about vehicle proximity that had
  nothing to do with the protected vehicle. `process_clip()` now takes an
  explicit `car_protection_applies` flag (computed the same way as the
  prompt's own car rules) and skips all vehicle-distance/depth/contact
  analysis entirely when it's False — the detected-classes listing itself
  still appears (that's generically useful), just never the vehicle
  distance language.
- **Fix: depth/contact analysis only ever considered a detected *person*
  near the vehicle, never an animal.** The base prompt has always flagged
  an animal jumping on, pawing at, or otherwise contacting the protected
  vehicle (e.g. a dog scratching a parked car) — but the more rigorous
  depth-estimation and pixel-level contact-segmentation stages silently
  excluded animals from consideration, so they never got that same level
  of scrutiny. `_best_person_vehicle_pair` (now `_best_subject_vehicle_pair`)
  considers dogs/cats/birds/horses as candidate subjects too, and the
  OBJECT DETECTION hint's distance wording now names the actual subject
  ("the detected dog's bounding box...") instead of assuming "person".

### New feature — object-tracking dwell/lingering signal

- Object detection's ByteTrack integration was tracking people across
  sampled frames but nothing used the resulting track IDs — the actual
  value tracking adds. Added a **TRACKING** prompt hint: when the same
  tracked person appears in most of the sampled frames, that's surfaced as
  a lingering/casing signal; when they appear in only one or two, that's
  surfaced as briefly passing through. Ambiguous cases emit no hint rather
  than a low-confidence guess. Requires `ai_object_detection_enabled`; no
  new dependency.

### Bug fixes — computer-vision pipeline install (found via an actual
Docker build + container run, not just review)

- **Fix: the entire CV pipeline silently failed to install.**
  `facenet-pytorch`'s pinned `Pillow<10.3.0` has no Python 3.13 wheel, so
  pip fell back to building Pillow 10.2.0 from source, which fails outright
  under current setuptools (`KeyError: '__version__'`) — and since all four
  packages were installed in one `pip install` call, this took down
  `ultralytics`, `opencv-python-headless`, and `transformers` with it even
  though none of them were actually the problem. Fixed by installing
  `facenet-pytorch` in its own step with `--no-deps` — its runtime code
  (MTCNN + InceptionResnetV1) only needs torch/torchvision/numpy/Pillow,
  all already installed by that point, and was confirmed by hand to work
  correctly against this project's actual Pillow>=12.3.0.
- **Fix: `torchvision` resolved an ABI-incompatible build.** Only `torch`
  was being installed from PyTorch's CPU-only wheel index; `torchvision`
  (pulled in by `ultralytics`) resolved from default PyPI instead, which
  targets mainline (CUDA) torch — despite satisfying the same version
  range on paper, this raised `RuntimeError: operator torchvision::nms
  does not exist` the moment `facenet_pytorch` (or anything else importing
  torchvision) was imported. Fixed by installing `torchvision` from the
  same CPU wheel index as `torch`, before `ultralytics` runs.
- **Fix: moondream's install silently downgraded this project's own
  Pillow requirement.** `moondream` pins `pillow<11.0.0`; pip's default
  resolver applied that pin on top of the `Pillow>=12.3.0` already
  installed from `requirements.txt`, only warning "incompatible" rather
  than failing. This project's own package install already runs last in
  the Dockerfile and re-upgrades Pillow back to >=12.3.0, so the shipped
  image was correct, but the ordering dependency is now documented inline
  so a future edit doesn't move the app-install step earlier and
  reintroduce the downgrade.
- All three were caught by actually building the Debian-based image end to
  end and running the container (health check, `/api/ai/faces`, Playwright
  e2e smoke check) rather than by code review alone — the non-fatal
  `|| echo "INFO: ..."` fallback around the CV pipeline install would
  otherwise have shipped a permanently-unavailable feature silently.

### Bug fixes — other

- **Fix: the web UI's "unsupported architecture" moondream notice was
  stale.** `_moondream_arch_supported()` hardcoded `x86_64`-only, dating
  from when Alpine/musllinux (not GPU availability) was the real
  constraint. Now that the base image is Debian, moondream's dependencies
  install on both amd64 and aarch64 (only local Photon *inference* still
  needs an NVIDIA/Apple Silicon GPU, checked separately at model-load
  time) — left unfixed, aarch64 users would have seen an inaccurate
  "not supported on this architecture" message even though the package
  now installs and could work on a GPU-equipped aarch64 host (e.g. Jetson).
- **Fix: a real face-enrollment photo would be rejected with an opaque
  413.** aiohttp's default 1 MB request-body limit is routinely exceeded
  by a single base64-encoded phone photo. Raised to 10 MB for this app's
  `web.Application`.
- Moondream is now attempted on both architectures (previously amd64-only)
  and installs after the computer-vision block so it reuses the CPU-only
  torch already installed there instead of pulling in a second,
  CUDA-enabled torch as a transitive dependency.

### Internal

- Added the `face_enrollments` table (`database.py`) and
  `add_face_enrollment`/`list_face_enrollments`/`delete_face_enrollment`.
- Added `/api/ai/faces` (GET/POST) and `/api/ai/faces/{id}` (DELETE) to
  `media_server.py`, plus the AI tab's enrollment panel.
- Added the `vision` optional dependency group to `pyproject.toml`
  (`ultralytics`, `opencv-python-headless`, `transformers`,
  `facenet-pytorch`) for local development/testing; the Docker image
  installs these directly (see Dockerfile) since HA add-on users can't run
  `pip install` themselves.
- CI's smoke-test job now also checks `/api/ai/faces` responds correctly
  inside the real built container.
- Bumped `numpy` (2.4.6 → 2.5.1) and `openai` (2.44.0 → 2.45.0) to the latest
  versions compatible with this codebase's own minimum-version floors (both
  already used unbounded `>=` constraints — nothing else needed bumping,
  everything else was already current); bumped the frontend's
  `typescript-eslint` dev dependency similarly. Verified via a full
  lint/typecheck/test run against the upgraded versions before committing.
- Removed several backend helpers left over from earlier refactors, once
  confirmed (via static analysis and a full cross-reference of every call
  site) to have no remaining callers anywhere in the app: `analyzer.py`'s
  `is_moondream_installed()` (superseded by `media_server.py`'s own
  `_is_moondream_installed()`, which also handles the persistent
  moondream-packages path), `database.py`'s `get_distinct_cameras()`
  (superseded by the richer `get_camera_stats()`) and `count_clips()`,
  `storage.py`'s `bytes_remaining()` and `apply_retention_policy()` (the
  app only ever calls its `apply_retention_policy_paths()` sibling), and
  four `vision.py` availability-check functions
  (`is_opencv_available`/`is_object_detection_available`/
  `is_depth_estimation_available`/`is_segmentation_available`) orphaned by
  the pre-5.0.0 consolidation of per-stage CV toggles into
  `ai_enhanced_detection_enabled` — only their sibling
  `is_face_recognition_available()` survived that consolidation, since
  face recognition kept its own dedicated toggle and Biometrics-tab
  affordance. Also removed `media_server.py`'s unused `download_path`
  constructor parameter/attribute. No behavior change; each removal was
  cross-checked against every call site (production and tests) before
  deleting, and the full test suite (99.35% coverage) passes unchanged.

### Bug fixes — CI (numpy dependency, config schema drift)

- **Fix: `numpy` was used unconditionally in `tests/test_vision.py` (and
  lazily but unguarded-for-pyright in `vision.py`) without ever being
  declared as a dependency**, so any CI environment that only installs the
  `test` extra (as `.github/workflows/ci.yaml` does) failed to collect
  `test_vision.py` (`ModuleNotFoundError: No module named 'numpy'`) and
  pyright failed with `reportMissingImports` on every `numpy`/`torch`
  import in `vision.py`. `numpy` is now a core dependency (`pyproject.toml`,
  `requirements.txt`) — it's lightweight and needed for real (not mocked)
  array/image plumbing in tests, unlike the heavy `vision` extra
  (torch/ultralytics/opencv/transformers/facenet-pytorch), which stays
  optional. The remaining bare `torch` imports in `vision.py` got the same
  `# type: ignore[import-not-found]` suppression already used on its
  `cv2`/`ultralytics`/`transformers` imports, since torch is still not
  installed in CI.
- **Fix: `config.yaml`'s `schema` section still listed the four pre-5.0.0
  CV toggles** (`ai_cv_preprocessing_enabled`, `ai_object_detection_enabled`,
  `ai_depth_estimation_enabled`, `ai_segmentation_enabled`) that were
  consolidated into `ai_enhanced_detection_enabled` for this release —
  `options` and `schema` had drifted out of sync (an option with no schema
  entry, and four schema entries with no matching option), which the HA
  add-on config validator rejects. `schema` now matches `options`/`config.py`
  exactly.

### Hardening — Docker image vulnerability count

- **Fix: an earlier setuptools/wheel security upgrade wasn't actually taking
  effect.** Both packages were reinstalled with `--upgrade --ignore-installed`
  to work around Debian's apt-managed `python3-wheel` having no pip RECORD
  file (which otherwise crashes a plain `--upgrade` and silently skips every
  package installed after it in the same block). `--ignore-installed`
  installs the new version without removing the old one first, though, so
  the old `setuptools`/`wheel` — and, since setuptools vendors its own
  bundled copy of `jaraco.context` internally, an old vendored copy of
  *that* too — stayed on disk and still showed up in a CVE scan even though
  nothing imported them anymore. `setuptools` now gets a plain `--upgrade`
  (it has a proper RECORD file, so this cleanly removes the old version's
  files, vendor bundle included); only `wheel` keeps `--ignore-installed`,
  with its now-superseded apt-owned copy explicitly deleted afterward.
- `apt-get upgrade` now runs right after `apt-get update`, before installing
  anything new, so already-installed base-image packages (e.g. `curl`,
  `libtasn1-6`) pick up whatever patched versions the base image's own apt
  sources already carry by build time — no new source added, nothing new
  installed, just existing packages caught up to already-available patches.
- Verified via `docker scout cves` against a full local build of both
  architectures: 301 → 278 vulnerabilities (12 fewer HIGH, plus a broader
  Medium/Low drop from the apt upgrade). The remaining CRITICAL findings are
  either baked into Home Assistant's own base image (a Go-compiled `tempio`
  binary, self-resolving once HA patches its upstream `trixie` base), an
  unavoidable `perl` transitive dependency of `postgresql-common` already at
  its latest available version, or have no fix published at any version yet
  (`transformers`) — none are fixable from this Dockerfile.

### Fixed — computer-vision pipeline model downloads on every add-on update

- **Fix: face recognition, object detection, depth estimation, and contact
  segmentation all re-downloaded their pretrained weights (~5-110 MB each)
  after every add-on update, not just once ever.** torch.hub
  (facenet-pytorch) and transformers/huggingface_hub (depth estimation,
  segmentation) both cache downloads under `$HOME/.cache` by default, and
  Ultralytics YOLO downloads to whatever directory the calling process's
  cwd happens to be — none of these are the `/data` volume, so a plain
  `docker restart` kept the cache (same container, same writable layer)
  but an add-on *update* (a new container from a new image) silently lost
  it, verified by hand. `TORCH_HOME`/`HF_HOME` now point at `/data`, and
  `ObjectDetector` resolves its model filename against that same
  persistent directory before handing it to `YOLO(...)`, since Ultralytics
  doesn't consult either env var itself.
- **Fix: enabling object detection could silently trigger a live `pip
  install` inside the running container.** `ObjectDetector` uses
  Ultralytics' `.track()` (not `.predict()`), which needs the `lap`
  package — not one of `ultralytics`'s own declared dependencies, only
  pulled in via its "AutoUpdate" self-healing fallback the first time
  `.track()` actually runs. Only surfaced by directly exercising object
  detection end-to-end (loading the model isn't enough to hit this — it
  only happens on the first real tracking call), not by anything the
  existing test suite's mocked-`ultralytics` tests could catch. `lap` is
  now installed alongside `ultralytics` at build time, so this never
  depends on the container having outbound network access at the moment a
  user's first clip is analyzed with object detection on.
- `e2e/smoke.mjs`'s tab-by-tab check was still only covering six of the
  eight nav tabs — missing exactly the two newest ones, Vehicles and
  Biometrics — so neither had ever actually been exercised by the
  automated Playwright smoke check that both CI and `scripts/smoke-test.sh`
  run. Added.

### Fixed — dark mode and modal styling

- **Card/InputText/Select/Textarea/FileUpload/Checkbox/Dialog ignored dark
  mode**, showing a white/light background under `.dark` throughout the
  Vehicles, Biometrics, and AI tabs. Unlike Button/Tag/Message/ToggleSwitch/
  Toast, Aura's presets for these components reference semantic tokens
  directly on a single flat `colorScheme`-less block — CSS custom
  properties are computed once at their `:root, :host` declaration and
  simply inherited, not re-evaluated per descendant, so the derived
  `--p-*-background` variable stayed frozen at the light-theme value no
  matter what `.dark` was doing elsewhere on the page. Added explicit
  `light`/`dark` `colorScheme` overrides for each of these components in
  `theme.ts`.
- **Modal/dialog backdrop turned fully black** instead of dimming the page
  behind it, most visibly on the "Clear stats?" confirm dialog. This app's
  dark palette is already very dark (`#0c0d16`-ish), and blending
  `rgba(0,0,0,0.6)` over a background that dark produces a color
  indistinguishable from solid black at any reasonable opacity — no amount
  of opacity tuning fixes it. Matched the app's own pre-existing
  `.modal-bg` pattern instead: `backdrop-filter: blur(3px)` on
  `.p-overlay-mask`, so the dimmed page is still visibly (if blurrily)
  there behind every PrimeVue overlay, in both themes.

### Fixed — spacing between adjacent interactive elements

- **Biometrics' "Enroll" button sat flush against the "Approve
  immediately" toggle above it** (0px gap) — the button had no
  `margin-top` of its own. Added `.enroll-submit-btn { margin-top:
  0.9rem }`.
- **Automations' notification-test result messages (email/Discord/mobile)
  had no spacing above or below**, PrimeVue's `Message` component ships
  with no default margin. Added `.channel-result { margin: 0.6rem 0 }`.

### Fixed — PostgreSQL startup logged a spurious FATAL on every boot

- **`FATAL: role "root" does not exist`** on every single add-on start:
  `services.d/blink-downloader/run`'s `pg_isready` readiness loop connected
  with no explicit role, so libpq fell back to the OS user running the
  script (root, since this service isn't `s6-setuidgid`'d the way the
  postgresql service itself is) — never a real database role here.
  Harmless (`pg_isready` only cares whether the server responds at all,
  and an auth rejection still counts), but alarming log noise on every
  cold start. Fixed with `-U blink`.
- That fix alone traded it for a different but equally spurious
  **`FATAL: database "blink" does not exist`**: without an explicit `-d`,
  libpq defaults the target *database* to match the *role* name, and the
  role (`blink`) and the actual database (`blink_clips`) are deliberately
  different names. Fixed with `-U blink -d blink_clips`; postgres startup
  logs are now completely clean, verified against a real HA Supervisor
  install.

### Fixed — issues found testing under a real Home Assistant Supervisor install

The fixes above (dark mode, spacing, postgres) were also found and
verified this way. Additional issues surfaced only by running the add-on
inside an actual Supervisor instance with real Blink/OpenAI credentials,
rather than the Vite dev server or a bare `docker run`:

- **The sidebar's connection badge showed "Unknown" instead of
  "Disconnected"** for the entire time Blink authentication was failing
  (bad credentials, still connecting, config error) — `extra_status`
  (the dict backing `/api/stats`'s `connected` field) was only ever
  populated on a *successful* connect, so a client that loaded the UI
  before that point saw no `connected` key at all rather than `false`,
  and the frontend only distinguishes "Disconnected" from "Unknown" when
  that key is present. `connected` now defaults to `False` from the
  moment the media server can answer requests, flipping to `True` only
  once Blink auth actually succeeds.
- **The Library page's Storage stat card overflowed its box** instead of
  wrapping, hard-clipped by an ancestor's `overflow: hidden` — its label
  reused `.lib-stat`'s `white-space: nowrap` (fine for the other cards'
  short static text like "Today") for a much longer, dynamic string
  ("Storage — 645.7 MB used · Free: 764.42 GB") that doesn't fit on one
  line at typical card widths. That one label now wraps normally.
- **The Models tab had no top padding and, along with Vehicles and
  Biometrics, no way to scroll to content past the viewport**: every
  other content tab has a `#page-<name> { overflow-y: auto; padding:
  1.75rem }` rule overriding `.page`'s own `overflow: hidden`, added
  individually as each tab shipped — these three simply never got one.
  Vehicles/Biometrics already padded themselves internally (at a
  slightly-off `1.5rem`, now `1.75rem` to match everyone else) so only
  needed the scroll fix; Models had neither and needed both.
- **AI Usage's stat tiles (Clips Analyzed, Total Tokens, etc.) ran
  noticeably larger and bolder than the equivalent numbers on the AI
  tab's Queue Status card** (`1.95rem`/800 weight vs. `1.5rem`/700) — the
  one visible font-scale inconsistency in the app once both tabs are open
  side by side. Sized to match.
- **A clip that failed AI analysis (rate limit, auth error, timeout,
  connection drop, ...) was silently recorded as a successful, "not
  suspicious" analysis with an empty summary, and never retried.** Every
  provider's `_call_model` returns `""` on failure (already logging the
  specific reason), but `analyze_clip()` fed that straight into
  `parse_response()`, which treats an empty string as a normal,
  confidently-not-suspicious result — permanently recording a false
  negative for a clip that was never actually analyzed, since
  `AnalysisQueue` only ever re-selects `status='pending'` rows. A clip
  that was genuinely suspicious could be silently marked safe. `""` after
  frames were successfully extracted is now treated as a hard failure
  (raises, so the caller correctly marks it "failed" instead of
  "completed" — visible in the AI tab's Queue Status card).
- **Hitting an OpenAI/Anthropic/Moondream Cloud rate limit mid-batch
  re-attempted every remaining clip in that batch anyway**, each one
  doomed to hit the exact same limit immediately (and each retrying
  internally via the provider SDK before failing) — pure wasted time and
  repeated `429` log spam for no benefit. `AnalysisQueue` now stops
  working through the current batch as soon as a rate limit is detected,
  leaving the rest pending for the next check cycle once the limit has
  had a chance to cool down.

### Fixed — a second round of real-account testing (real Blink + OpenAI credentials)

- **Every PrimeVue form control (Button/InputText/Select/Textarea/
  FileUpload/Message) across Vehicles, Biometrics, and the confirm
  dialog rendered noticeably larger than the rest of the app** —
  Aura's own default size is close to 40px/16px text, and even the ones
  already passing `size="small"` (0.875rem/0.625rem padding) were still
  visibly bigger than this app's pre-existing hand-rolled small-button
  scale, since nothing had ever overridden PrimeVue's own tokens to
  match it. Card/Dialog titles (1.25rem) and Tag (0.875rem, no `size`
  prop to opt out of at all) were similarly oversized next to every
  hand-rolled heading/badge elsewhere. `theme.ts` now overrides the
  shared `form.field.sm.*` tokens those components' `size="small"`
  variant reads from, plus direct Card/Dialog/Tag overrides where no
  size prop exists — and every control that was still on the unstyled
  default now explicitly passes `size="small"` so it benefits from them.
- **The sidebar's Cameras section (per-camera clip counts) only ever
  showed on the Library tab**, unlike every other persistent sidebar
  element (the connection badge, Sync, Refresh) — switching tabs hid it
  entirely even though the data was already loaded. Now shown on every
  tab once cameras have loaded; clicking a camera from elsewhere both
  applies the filter and switches to Library, so the click still has a
  visible effect.
- **Clip-card selection checkboxes couldn't be clicked** — they rendered
  unconditionally but had no click handler of their own, so the click
  always bubbled up and opened the clip modal instead. Selecting a clip
  only ever worked by clicking "Select" first, with no hint from the
  checkbox itself that that was required. It now stops propagation and
  emits its own event; clicking it enters select mode (if not already
  active) and toggles that clip in one click.
- **Clicking "Prompt" in a clip's AI panel appeared to do nothing** —
  it actually opened, just rendered behind the already-open clip modal.
  `ClipModal` is deliberately `<Teleport to="body">`'d (so it survives
  its tab going `display:none` on switch), landing it as a sibling of
  `#app` rather than nested inside it, while `PromptOverlay`/
  `HelpOverlay` render inside `#app` at the same default z-index —
  opening either from within an open clip modal put it behind that
  modal. Both now use z-index 150, matching the precedent
  `TwoFAOverlay` already set for the same reason. That surfaced a second
  bug once fixed: stacking two overlays' own 82%-opaque + blur(3px)
  backdrops compounds to ~97% opaque — the same "turns fully black" bug
  as before, just two layers deep — so these two specifically now use a
  lighter backdrop, since `ClipModal`'s own already dims the real page
  underneath both.
- **A several-hundred-clip thumbnail backlog took hours to catch up**
  after enabling `download_thumbnails` (5 clips backfilled per poll
  cycle) — long enough that it read as broken rather than gradually
  working. Raised to 15/cycle; each is a sub-second ffmpeg
  `-frames:v 1` extract, so this still leaves plenty of margin in a
  300s+ poll cycle.
- **`ai_object_detection_model`'s description explained the n/s/m/l/x
  size tradeoff but never showed the actual value to type** (e.g.
  `yolo11n.pt`) — a user reading it had no way to know the expected
  format. Both the add-on's Configuration tab description and the
  `config.yaml` comment now spell out the exact filenames.

### Fixed — a third round of real-account testing (reviewing actual prompts/responses)

- **The AI prompt's "time of day" context used the clip's raw UTC hour**
  instead of local time — a clip at 17:10 in a UTC-3 timezone (broad
  daylight) showed up in the prompt as "night (20:10 UTC)", working
  directly against the prompt's own instruction to factor time of day
  into the suspicious/not-suspicious judgment. Now converts to the
  container's configured local timezone first (the same one
  `ai_schedule_start`/`end` and `digest_time` already rely on).
- **The background analysis queue hit the AI provider's real API every
  single idle poll cycle** (default every 60s) just to immediately find
  nothing to do — its health-check cache (30s) was shorter than the
  poll interval, so it never actually prevented a call. Now checks the
  local pending count first and skips the real network call entirely
  when there's nothing queued.
- Added a connection-state icon (wifi / times-circle / question-circle)
  to the sidebar's Connected/Disconnected/Unknown tag — a plain
  color-only badge was easy to miss at a glance.

### Fixed — a fourth round of real-account testing (short viewports, mobile)

- **The sidebar's camera list (and everything else in the sidebar) could
  be silently squeezed down to a sliver on a shorter screen.** Every
  direct child of the sidebar's column flex layout defaulted to
  `flex-shrink: 1` with no minimum, so on a short viewport the layout
  algorithm shrank the camera list (and brand/tabs/utility rows) below
  their own `max-height`/content size to make everything fit — on a
  660px-tall viewport this crushed the camera list down to ~29px, hiding
  every camera but "All Cameras" with no visible scrollbar to hint more
  was there. Pinned every fixed-size section to `flex-shrink: 0` so only
  the deliberately-flexible spacer compresses; the sidebar as a whole now
  scrolls (it already had `overflow-y: auto`) if content still doesn't
  fit after that, rather than one specific section vanishing.
- **The Vehicles and Biometrics tabs overflowed horizontally on a phone-width
  screen**, clipping paragraph text and form fields off the right edge of
  the viewport instead of wrapping. Both pages' top-level containers were
  missing the `min-width: 0` that `.auto-content` (used by
  Automations/AI/Models) already has — flex items default to
  `min-width: auto`, refusing to shrink below their content's natural
  width unless told otherwise.
- **A raw, unformatted Ultralytics warning and settings-file notice printed
  straight to stdout** the first time the object-detection model loaded
  each container start, bypassing the add-on's own structured logging.
  It happens because `$HOME/.config/Ultralytics` isn't writable by the
  add-on's runtime user; ultralytics falls back to `/tmp` on its own, but
  announces the fallback with unlabeled print statements. Now points
  `YOLO_CONFIG_DIR` at the same persistent, already-writable directory the
  model weights cache under, so no fallback (and no notice) is needed, and
  the settings file survives a container recreation like the weights do.
- **HA persistent notifications ("HA Notifications") are now off by
  default.** With `notify_ha` previously defaulting on, a real,
  continuously-syncing Blink account generated a notification per
  downloaded clip — signal quickly turning into noise. Existing installs
  that already have this explicitly enabled are unaffected; this only
  changes the default for new installs.
- **Library tab polish from real-account feedback**: the top stat badges
  (Today/This week/Total/Starred/Library size/Storage) now center their
  label and number instead of left-aligning; the Source and Tag filter
  dropdowns show "All sources"/"All tags" placeholder text; the plain
  "Loading…" text while clips load is now a themed spinner; and the bulk
  "★ Star all"/"🗑 Delete all" buttons — which only ever acted on
  individually-checked clips, doing nothing when none were checked — are
  now "★ Star selected"/"🗑 Delete selected" alongside a real "Select all
  N" action that selects every clip currently loaded in the grid.

### Fixed — a fifth round of real-account testing (car-zone accuracy, mobile top bar)

- **A person standing right next to (but not overlapping) a protected
  vehicle could score near-zero "zone motion"**, causing the ZONE MOTION
  prompt hint to actively tell the AI "away from the protected vehicle's
  usual spot" for exactly the near-miss case it most needs to catch.
  `VehicleZonePicker`'s only instruction is to draw a box around the
  vehicle itself, so the zone is normally a tight fit around the car's own
  footprint — a person standing beside it generates motion pixels adjacent
  to, not inside, that box. `_zone_motion_fraction` now measures motion
  within the zone padded outward by 20% of its own width/height, so
  immediately-adjacent activity registers as zone-relevant instead of
  reading as unrelated background motion elsewhere in frame. (Confirmed via
  a real clip during testing that this specific hint — not a detection
  failure — was what pushed a near-vehicle event to a confident
  not-suspicious verdict.)
- **Object detection model (`ai_object_detection_model`) can now be picked
  from a dropdown** in the Supervisor's Configuration tab, matching how
  `ai_provider` already works, instead of requiring the exact filename
  (`yolo11n.pt`/`yolo11s.pt`/`yolo11m.pt`/`yolo11l.pt`/`yolo11x.pt`) to be
  typed by hand.
- **The mobile top bar's Refresh/Sync buttons stacked into a two-line
  column** (Refresh above Sync) inline within the same row as the
  connection badge and icon buttons, making that row roughly twice as tall
  as everything beside it and the whole header noticeably bulkier than it
  needed to be on a phone screen. They're now icon-only in a single row
  under 600px width, matching the icon-only treatment the nav tabs already
  get at that breakpoint (tooltips/`aria-label`s still carry the text).
- **The computer-vision pipeline (object detection/tracking, depth,
  contact segmentation, face recognition) and the car-zone check gave no
  indication in the logs — even at debug level — that they ran at all**,
  beyond a one-time "model ready" line the first time each model loaded.
  Real per-clip results (detected classes, tracking, depth/contact, face
  match counts) were reaching the AI prompt correctly the whole time, but
  there was no way to see that from the logs, making it impossible to
  confirm the features were doing anything without manually inspecting a
  stored prompt. `analyze_clip` and `VisionPipeline.process_clip` now each
  log one DEBUG summary line per clip — whether a car zone was found for
  that camera and what fraction it computed, and what every enabled vision
  stage actually detected. Face recognition results are logged as counts
  only (never names), matching the same name-free guarantee the prompt
  hint itself already follows. The motion-trajectory ("smart brain"
  approaching/retreating) hint had the exact same problem and now gets its
  own DEBUG line too.
- **Routine coming and going through a non-vehicle access-point camera
  (front door, back door, etc.) was getting flagged suspicious more than
  it should**, with descriptions like "attempting to unlock it" or "trying
  to access the mailbox" for completely ordinary behavior the prompt's own
  rules already say is not suspicious. Root cause: every OpenAI reasoning
  model (o1/o3/o4/gpt-5 family) was hardcoded to the lowest
  `reasoning_effort` tier for every analysis, on the theory that
  suspicious/not-suspicious classification is a short, well-defined task.
  Real-account testing didn't bear that out — correctly weighing "resident
  using their own door normally" against a long rule list with several
  deliberate "favor flagging when in doubt" carve-outs needs more than the
  minimum effort tier to apply reliably. Raised to `"medium"` (the OpenAI
  API's own default), trading a modest amount of latency/cost for fewer
  false positives on this class of judgment call.
- **`ai_escalation_provider` now shows a dropdown** in the Supervisor's
  Configuration tab (same provider list as `ai_provider`, plus a blank
  option to leave escalation disabled) instead of requiring the exact
  provider string typed by hand.
- **Debug-level logs were dominated by the `openai` SDK's own internal
  request/response tracing** — including the full base64 image data for
  every frame and the complete prompt text, logged in full on every single
  analysis call — plus raw per-byte connection tracing from `httpcore`
  underneath it. Together these buried this add-on's own debug output (the
  per-clip vision-pipeline/car-zone/trajectory lines) under what was
  effectively an unreadable wall of SDK internals and duplicated image
  data. Both loggers are now capped at WARNING regardless of the add-on's
  own `log_level`, so genuine SDK-level problems (retries, deprecation
  notices) still surface, but routine per-request tracing doesn't.
  `httpx`'s own logging is untouched — it only ever contributes one
  concise, useful line per request.

### Fixed — a sixth round of real-account testing (install-blocking config.yaml bug)

- **`config.yaml` had a committed `image: ghcr.io/brianbaggs35/blink_clip_downloader`
  field, which would have broken installation for every real user.** When an
  add-on's `config.yaml` declares `image:`, Home Assistant Supervisor pulls
  that exact tag from the registry instead of building from the bundled
  `Dockerfile`/`build.yaml` — it never falls back to a local build if the
  pull fails. No CI job in `.github/workflows/ci.yaml` publishes to that (or
  any) registry — the `build` job only runs a local `docker build` and
  `docker save`s the result as a workflow artifact for `smoke-test` to
  consume, with `permissions: contents: read` (no `packages: write`). A
  Supervisor install attempt against a freshly recreated test environment
  reproduced this directly: `Failed to fetch manifest ... 404` followed by
  `manifest unknown`. Removed the `image:` field so Supervisor builds the
  add-on locally from `Dockerfile`/`build.yaml` — the standard distribution
  model for a repository add-on installed via *Settings → Add-ons →
  Repositories*, and the only one CI actually supports.
- **`ai_escalation_provider`'s shipped default (`""`) failed HA Supervisor's
  own schema validation, blocking every fresh install from starting at
  all** — a regression from the `ai_escalation_provider` dropdown fix
  earlier this round. That fix correctly changed the option's *type* from
  `str?` to the optional enum `list(ollama|...|openai)?` so the
  Configuration tab renders a real dropdown, but left the *default value*
  as `""`. Unlike `str?`, an optional `list(...)?` only accepts "unset" as
  a fully absent key — a stored `""` fails with `value must be one of
  [...]`. Reproduced directly against a real Supervisor instance (`ha apps
  start` refused to start the add-on with this exact error) before being
  traced back to the shipped default. Removed the default line entirely
  (the app's own options-loading in `config.py` already treats a missing
  key as `""` internally, so no Python change was needed) rather than
  changing the value, since the field's whole point is "unset by default."
- **Activating a Moondream Cloud fine-tuned checkpoint from the AI tab's
  Fine-Tune card didn't survive an add-on restart.** `set_finetune_model`
  only ever updated the live analyzer's in-memory state; nothing persisted
  the activated model id anywhere, so a restart silently reverted to
  whichever `moondream_finetune_model` (if any) was last saved in
  `options.json` — defeating the purpose of activating a checkpoint in the
  first place. Fixed by mirroring the existing `camera_configs.json`/
  `vehicle_settings.json` pattern: activation now also writes to a new
  `finetune_state.json`, which takes priority over the
  `moondream_finetune_model` option once written (see
  `app.py`'s `_load_finetune_model_from_ui`).
- **Toast notifications for short messages (e.g. "Camera configs saved")
  looked off-center** — a wide empty gap sat between the message and the
  close button. Root cause: PrimeVue's Aura theme hardcodes a flat 25rem
  width for every toast regardless of message length. Overrode it to
  `fit-content` in `theme.ts`, following the same pattern already used
  there for Tag/Card/Dialog's other oversized Aura defaults, plus a
  `max-width` cap (`base.css`) so long messages still wrap instead of
  overflowing on mobile. Also removed the old hand-rolled `.toast` CSS
  class this replaced — confirmed dead, no template has referenced it
  since the toast host moved to PrimeVue's own `<Toast>` component.
- **Success toasts showed two checkmarks** — PrimeVue's `success` severity
  already renders its own check icon, but five toast messages
  ("Camera configs saved ✓", "Signed in to Blink ✓", etc.) also had a
  literal `✓` baked into the text. Removed the redundant character from
  all five.
- **Several tabs (Automations/AI/AI Usage/Models/Status/Vehicles/
  Biometrics) had content sitting flush against the bottom edge when
  scrolled all the way down**, unlike the Library tab. Bumped
  `padding-bottom` on each page container from the shared 1.75rem to
  3rem.
- **Plain "Loading…" text replaced with a spinner** (matching the
  Library tab's own pattern from an earlier round) on every *page*-level
  loading state: the AI tab, AI Usage, Status, Vehicles, Biometrics, the
  Suspicious Activity Feed, and the AI tab's Camera Configurations
  section. New shared `components/layout/LoadingIndicator.vue` avoids
  repeating the same spinner+label markup at each of those seven call
  sites. Left as plain text: two genuinely small inline status rows
  (`ClipAiPanel`'s and `FineTuneCard`'s own compact "Loading…", both
  0.8rem font in a tight flex row) where a full-size spinner would look
  disproportionate rather than better.

### Improved — Biometrics face enrollment clip picker

- **`EnrollFromClipPicker` (Biometrics tab) only ever offered clips from a
  fixed, uncustomizable recent window**, so on an actively-recording
  camera the available thumbnails could all be nighttime/IR shots with no
  way to reach back further for a better-lit frame. Added a "Show clips
  from" dropdown (6h / 24h / 48h / 7 days, default 24h) next to the
  existing camera picker, and raised the clip count from 8 to 24 so a
  wider window actually surfaces more choices instead of just spacing the
  same 8 further apart.

### Added — face-bypass auditability, and PrimeVue Select dark-mode fix

- **No way to tell whether the suspicious-flag face-recognition bypass was
  firing correctly** — it's designed to be all-or-nothing and never names
  anyone in a prompt (see `analyzer.py`'s `_face_bypass_applies`), but that
  also meant there was no way to *see* it working, or catch it if it were
  ever bypassing for the wrong person. Every bypass now logs a local-only
  INFO line naming the matched approved member(s) and the clip/camera, and
  `analysis_results` gained `face_bypass_applied`/`face_bypass_names`
  columns so this is queryable, not just grep-in-the-logs. The Biometrics
  tab's new "Face-bypass activity" card (`GET /api/ai/faces/bypass-stats`)
  shows the running total, a per-person breakdown, and recent events —
  local-only display, same as everything else on that tab.
- **PrimeVue `Select`'s dropdown panel ignored dark mode**, always
  rendering with a white background and dark text regardless of the active
  theme — the same root-cause pattern already fixed here for Card/
  InputText/Checkbox/Dialog (`theme.ts`'s dark-mode-color-scheme comment):
  the panel is a separate `overlay` token section from the closed input
  box's `root`, and only `root` had an explicit dark declaration. Fixed by
  adding the equivalent `overlay.select.*` override.

Confirmed already correct, no change needed:
- **Face recognition already matches across every camera, not just the one
  a person was enrolled from** — `FaceRecognizer.recognize()` and
  `list_face_enrollments()` have no camera parameter or filter at all
  (`face_enrollments` doesn't even have a camera column), so an enrollment
  is inherently global.
- **Removing an enrolled person already uses the app's real confirmation
  modal** (`useConfirm()`/`useConfirmStore`), not a native browser prompt.

### Added — pagination for the face-enrollment clip picker

- **`EnrollFromClipPicker`'s clip strip had no way to look further back**
  than whatever the current time-range window returned — if none of those
  clips had a clean shot of the face, there was nowhere else to go. Added
  ‹/› paging buttons flanking the thumbnail strip (offset-based, reusing
  `listClips`' existing `offset` param), with the outer arrow disabled once
  a page comes back short (the signal that there's nothing older left) and
  the inner arrow disabled at the first page. Changing camera or the
  lookback window resets back to the first page rather than keeping a now
  out-of-context offset.

### Fixed — 🔔 notified badge stuck on after a clip was correctly re-analyzed

- **A clip re-analyzed to "not suspicious" (e.g. via the Library's
  Re-analyze button, or after a prompt/model fix like this round's
  `reasoning_effort` change) kept showing the 🔔 notified badge forever.**
  Root cause: `add_analysis_result` always inserts a new row rather than
  replacing the previous one, so a re-analyzed clip has multiple
  `analysis_results` rows — but `get_clips`' `notified` computation checked
  whether *any* row was ever suspicious, not just the current one.
  Reproduced directly against real data: one of this session's own
  test clips had exactly this row history (`{suspicious, not-suspicious}`)
  and was still showing as notified despite its current, correct verdict
  being "clear." Scoped the check to only the clip's most recent
  `analysis_results` row, matching `get_analysis_for_clip`'s own
  "latest wins" semantics.

### Fixed — reconciled clips showed no Duration in the Library

- **A clip re-imported by the startup library scan (`library_scanner.py`,
  triggered when `/data`'s database is wiped but `download_path`'s files
  survive — e.g. an add-on reinstall) always showed Duration as "—".**
  Unlike the normal download path (which reads `duration` straight off the
  Blink API's own clip response), reconciliation only ever had the bare
  file to work with and hardcoded `duration: 0` rather than reading it
  from the file itself. Duration is embedded in every video file's own
  container metadata though, so — unlike `source` (a Blink-side "how was
  this clip triggered" fact with no equivalent anywhere in the file
  itself, which necessarily stays blank for reconciled clips) — it's
  genuinely recoverable. Reconciliation now probes it with `ffprobe`
  (already implicitly available — the add-on's AppArmor profile already
  allows executing anything under `/usr/bin/`, where `ffprobe` ships
  alongside the `ffmpeg` this add-on already depends on), falling back to
  the existing "—" placeholder if a probe ever fails.

### Fixed — ★ Starred count stat stuck until a manual page refresh

- **Starring/unstarring a clip (from the modal or in bulk) updated the
  clip's own star icon immediately but left the "★ Starred" stat badge at
  the top of the Library tab showing the stale count** until the whole
  page was reloaded. The per-card patch (`onStarred`) and the bulk-star
  flow (`bulkStar`) both updated `clips` but never touched the separately-
  loaded `stats` aggregate. `onStarred` now adjusts `stats.starred_count`
  by ±1 in place (only when the clip's starred state actually changes, so
  a duplicate event for an already-starred clip can't double-count), and
  `bulkStar` now reloads stats alongside its existing clip-list reload.

### Fixed — toast checkmark icon not vertically in line with the message

- **Toast messages put their text in PrimeVue's `detail` field, leaving
  `summary` unset — but `ToastMessage.vue` always renders the `summary`
  `<span>` unconditionally (only `detail` is behind a `v-if`).** That left
  an empty-but-still-laid-out line above every message, which pushed the
  visible text down a line while the severity icon stayed aligned to the
  top of the whole block — reading as the checkmark sitting above/outside
  the text rather than next to it. Toasts now populate `summary` (the
  field this template actually expects to always be present) and leave
  `detail` unset, eliminating the phantom line entirely.

### Fixed — sidebar Connected badge and icon row left-aligned, not centered

- **The Connected/Disconnected badge and the theme/help/notification icon
  buttons above the Refresh/Sync buttons were left-aligned**
  (`.app-nav-conn-tag { align-self: flex-start }`, `.app-nav-icon-row` with
  no `justify-content`), leaving visibly unused space to their right —
  inconsistent with Refresh/Sync directly below them, which are full-width
  and center their own label/icon. Both now center within the sidebar
  column (`align-self: center` on the tag, `justify-content: center` on
  the icon row) to match.

### Improved — Library storage stat now states the configured quota as text

- **The Library tab's compact "Storage" stat only ever described free disk
  space in words** (e.g. "Storage — 1270.8 MB used · Free: 785.41 GB") —
  the configured `max_storage_gb` quota was only visible via the thin
  progress bar underneath, easy to miss, and the label made it read as if
  the whole disk were available regardless of quota. The backend has
  always returned `quota_gb` alongside `free_gb` (`storage.py`'s
  `disk_stats()`), and the Status tab's own Storage card already showed it
  as an explicit "Quota" row — the Library stat just never used the field.
  It now reads "Storage — 1270.8 MB used of 10.0 GB quota · 785.41 GB free
  on disk" whenever a quota is configured, falling back to the prior
  disk-only wording when it isn't (`quota_gb: 0` — no quota configured).

### Fixed — page loading spinner appeared top-left on the Status tab only

- **`LoadingIndicator.vue`'s own wrapper only centered its children
  (spinner + label) *within itself* — but as a plain block element it
  shrink-wraps to its content's width, so `align-items: center` had
  nothing wider to center against.** Gave the shared component
  `width: 100%` so it fills whatever container it's placed in, which is
  the right fix in general — but the Status tab's own loading/error
  states are direct children of `.page.active` (`display: flex`), and a
  flex item with no explicit width shrink-wraps to its content regardless
  of what its own children ask for, so the shared-component fix alone
  couldn't reach it. Every other tab avoids this because its loading
  state sits inside its own already-`width: 100%` block wrapper
  (`.auto-content` for AI/AI Usage/Automations/Models, `.vehicles-page`/
  `.biometrics-page` for Vehicles/Biometrics) — normal block children
  fill their container's width by default, so the shared-component fix
  was sufficient there. Status has no such wrapper, so its loading/error
  `<div>`s now also set `width: 100%` directly, giving them a definite
  width to fill the row before `LoadingIndicator`'s own centering takes
  over. Audited every other tab and their nested cards
  (`CameraConfigsSection`, `SuspiciousFeed`, `FaceBypassActivityCard`) —
  all already sit inside a `width: 100%` block wrapper and needed no
  change.

### Investigated — "the Prompt button isn't showing on the clip modal anymore"

- **Confirmed not a regression.** `ClipAiPanel.vue`'s "📝 Prompt" button is
  intentionally gated behind `promptDebugEnabled`, wired end-to-end from
  `ai_prompt_debug_enabled` (`LibraryPage.vue` → `ClipModal.vue` →
  `ClipAiPanel.vue`) — confirmed live against the real add-on instance
  (`ai_prompt_debug_enabled: false`), which is also its schema default
  (`config.yaml`). The wiring is intact; the button is legitimately hidden
  because the setting is off, not broken. It has no toggle anywhere in the
  web UI yet, though — the only way to reach it is the raw HA Supervisor
  Configuration tab, which explains why it can feel like it "disappeared"
  after being on for a stretch of testing without leaving an obvious way
  back. Not addressed further here (a proper web UI toggle for it is a
  larger change than this round's scope); the empty-state fix below covers
  the concrete rest of the request.

### Fixed — Prompt overlay showed a raw fallback sentence inside a code block

- **When a clip's analysis ran without prompt-debug on, clicking "📝
  Prompt" (once visible — see above) rendered `usePromptOverlayStore`'s
  hardcoded `'No prompt was captured for this clip.'` fallback string
  inside the same monospace `<pre>` block used for real prompt text** —
  read as an odd, code-styled non-answer rather than an explanation. The
  store now passes through whatever it's given, unsubstituted, and
  `PromptOverlay.vue` renders a proper explanatory message in its place
  when empty: prompt-debug was off when that analysis ran, and since the
  button is only reachable when it's on *now*, re-analyzing the clip will
  capture it.

### Added — cap auto-analysis to the newest few clips in a download burst

- **A genuine backlog burst — a fresh install's first poll pulling a busy
  24h window, or catch-up after the add-on being down for a while — used
  to enqueue every single downloaded clip for AI analysis unconditionally,
  with no cap distinct from `max_clips_per_poll` (default 50).** Downloads
  are already reasonably bounded, but analysis costs real per-clip API
  tokens, and nobody wants dozens of already-hours-or-days-old clips
  auto-analyzed the moment the add-on starts. `_on_clips_downloaded` now
  auto-queues only the `_MAX_AUTO_ANALYZE_BURST` (5) most recent clips —
  by timestamp, not download order — whenever a single batch exceeds that;
  every clip in the batch still downloads, gets its notification/webhook/
  manifest entry, and appears in the library exactly as before, just
  without automatic analysis for the older ones. Analyzable on demand any
  time via the existing "Analyze Now" button. Routine polling (normally
  1-2 new clips) is unaffected — the cap only ever changes behavior when a
  batch is larger than 5.
- **Investigated separately: the "only pull the last 24h unless the
  database already has more" behavior already existed** —
  `download_new_clips()` has always derived its Blink API `since` filter
  from the download tracker's persisted cursor (`/data/downloaded_clips.json`),
  falling back to 24h-ago only when that cursor is genuinely absent (a
  fresh tracker). A normal restart/upgrade keeps the real cursor (however
  far back it goes); only a full wipe (uninstall, or `/data` loss) resets
  it to the 24h default — and even then, `library_scanner.py`'s
  reconciliation of surviving clip files never enqueues analysis on its
  own (confirmed by code inspection: it only calls `db.add_clip`, never
  the analysis queue), so a reconciled backlog was never actually at risk
  of a token-burning analysis flood in the first place. No change needed
  here beyond the auto-analyze cap above.

### Improved — AI Connection card now visually separates tier-1 from tier-2

- **The AI tab's AI Connection card interleaved tier-1 (primary model) and
  tier-2 (escalation model) controls with no visual grouping** — the
  escalation box sat between the plain Provider/Model text and the tier-1
  fetch/select/copy buttons, so the two tiers' controls didn't even appear
  next to each other, let alone read as distinct groups. Reordered so all
  tier-1 content (Provider/Model, the model picker, Moondream local
  install) is contiguous, followed by tier-2's escalation box (only when
  `ai_escalation_provider` is configured), each now under its own small
  uppercase divider label ("🎯 Tier 1 · Primary Model" / "🪜 Tier 2 ·
  Escalation Model") matching the sidebar's existing section-label style.

### Changed — fresh-connect download lookback shortened from 24h to 6h

- **A reconnect with no download history yet (first install, or after
  re-authenticating the Blink account) pulled clips from as far back as 24
  hours, on top of the 5-clip auto-analyze cap added earlier this round.**
  Six hours comfortably covers "what happened while this was being set
  back up" with much less chance of a large backlog landing on the very
  first poll; anything older is still reachable manually via "Analyze
  Now" once it's downloaded. A normal restart/upgrade is unaffected — this
  only applies when the download tracker has no persisted cursor at all.

### Added — bulk-select multiple clips and analyze them together

- **New "🔬 Analyze selected" action in the Library tab's bulk-select bar**,
  alongside the existing Star/Delete/ZIP actions — select multiple clips
  and analyze all of them without opening each one individually. Gated
  behind a confirmation dialog stating exactly how many clips will be
  analyzed (matching the existing bulk-delete confirmation pattern) since,
  unlike starring or deleting, this spends real AI provider tokens. Capped
  at 25 clips per batch (matching the existing ZIP-export cap) and run
  **sequentially**, not in parallel like the other bulk actions — unlike
  the background analysis queue (used for normal downloads), which already
  throttles and backs off on rate limits, firing many concurrent requests
  at the synchronous per-clip analyze-now endpoint would have no such
  protection. Only shown when AI analysis is actually enabled/configured.
  A per-clip failure doesn't abort the rest of the batch (same as
  bulk-delete); the final toast reports how many succeeded.

### Verified — release build/publish workflow, tier-1-only, and disabled-feature paths

- **Full review of `.github/workflows/build.yaml`** (triggered on `release:
  published` / manual dispatch): confirmed the per-arch matrix
  (aarch64/amd64) and base images match `build.yaml`'s own `build_from`
  mapping, the `Dockerfile`'s three `ARG`s (`BUILD_FROM`/`BUILD_ARCH`/
  `BUILD_VERSION`) line up exactly with the build-args the workflow passes,
  and the multi-arch manifest step correctly combines both per-arch pushes
  into a single `:VERSION` (and `:latest`) tag. No bugs found in the
  workflow itself. One drive-by fix: `Dockerfile`'s `BUILD_VERSION` default
  (only used for a manual `docker build` with no `--build-arg` — every real
  build, CI or HA's own, always passes it explicitly) was stuck at a stale
  `2.8.1`, now `5.0.0`. Two things worth the user's attention that aren't
  code fixes: (1) the workflow reads its version purely from `config.yaml`,
  not the git release tag — nothing cross-checks that a release's tag name
  actually matches `config.yaml`'s `version:`; (2) whether these published
  images ever get pulled by a real install depends on `config.yaml` having
  a matching `image:` field (currently absent — see the CI-verification
  entry earlier this round) and the GHCR package's visibility being set to
  public, a one-time GitHub web UI step this workflow doesn't (and can't)
  control.
- **Verified tier-1-only operation** (no escalation model configured) both
  by code (`BaseAnalyzer._maybe_escalate`'s very first line is
  `if not response or self._escalation_analyzer is None: return response`
  — a true no-op, no side effects, before touching anything escalation-
  related) and against real historical data: queried the live database for
  `analysis_results` rows with an empty `escalation_provider` and confirmed
  well-formed `is_suspicious`/`confidence`/`summary` values across several
  real clips.
- **Verified optional advanced features degrade cleanly when disabled**:
  `VisionPipeline.process_clip` gates enhanced detection and face
  recognition behind their own config flags (both `False` by default), and
  the heavy per-stage dependencies (`ultralytics`, etc.) are only imported
  inside each stage's actual detection call — never at `VisionPipeline`
  construction time, which happens unconditionally on every startup
  regardless of whether any stage is enabled.

### Improved — smoke-test coverage for the newer tabs

- **`e2e/smoke.mjs` (the script CI's smoke-test job actually runs) already
  derives its tab list from `AppSidebar.vue`'s own `TABS` array, so it was
  already clicking through Vehicles and Biometrics along with everything
  else** — no change needed there. `e2e/verify.mjs` (an ad-hoc dev/AI
  screenshot helper, not run in CI) had a separate, hand-maintained tab
  list that had gone stale from before those two tabs existed; updated to
  match. CI's own endpoint smoke checks already covered
  `/api/ai/faces` (Biometrics) but nothing Vehicles-specific — added
  `/api/vehicle/settings` and `/api/ai/camera-configs` alongside it.

### Fixed — Connected badge stuck until visiting Library or Status

- **The sidebar's Connected/Disconnected badge only ever updated as a side
  effect of Library's or Status's own `/api/stats` polling** — if the app
  opened straight to a different tab (e.g. AI) while the Blink connection
  was still being established, the badge stayed at whatever it started
  as until the user happened to navigate to one of those two tabs, or
  reloaded the page. `AppSidebar.vue` is the one thing that's always
  mounted for the app's whole lifetime, so it now owns a small
  independent poll (every 10s, plus once immediately on mount) of its
  own, matching what the store's own doc comment already claimed
  ("a cross-cutting concern surfaced in the shell, not owned by one tab")
  but the implementation didn't actually do.

### Redesigned — Library storage widget, and a follow-up dark-mode contrast fix

- **The Library tab's storage stat crammed three pieces of information
  into one wrapped label above an oversized (Aura's default 1.25rem)
  progress bar**, reading as cramped and visually heavy against its
  sibling stat cards. Restructured into a small title ("💾 Storage"), a
  prominent used/quota line, a secondary free-space line, and a
  noticeably slimmer 0.4rem bar. Re-confirmed while doing this that the
  bar's percentage was already quota-based (`used_bytes / quota_bytes`),
  not total-disk-based — no calculation change needed, just the layout.
  Text contrast improved along the way: the free-space line now uses
  `var(--text-dim)` instead of `var(--muted)`, which read as too low-
  contrast at this size.
- **Found and fixed a second, related dark-mode contrast bug while
  investigating a report about dropdown option text being hard to
  read**: `Select`'s dropdown *panel* background was already fixed
  earlier this round (`overlay.select.*`), but the individual *option
  row text* is a separate token section (`option.color`, referencing
  `{list.option.color}`) that hadn't been re-declared under an explicit
  `colorScheme.light`/`dark` split — same root cause as every other
  Aura dark-mode fix this release (a flat component-level token only
  ever evaluated once, under `:root`, in the light scheme), just a
  different field within the same component than what was fixed before.
  Option text was landing at its light-mode color, unreadably dim
  against the now-correctly-dark panel.

### Changed — `download_thumbnails` now defaults to on

- **`download_thumbnails` defaulted to `false`, but the Vehicles tab's
  car-zone picker (both browsing clips and drawing the zone) and the
  Biometrics tab's clip-browsing strip both display a clip's thumbnail
  directly** — without one, they show a broken image with no way to
  pick a clip at all. (Biometrics' actual frame-extraction-for-
  enrollment step is unaffected either way — it pulls frames from the
  video file on demand, per `/api/clips/{id}/frames` — but the strip you
  use to *choose* a clip in the first place still needs the thumbnail.)
  Now defaults to `true` so both tabs work without a manual settings
  change first; `config.yaml`'s description text (shown by Supervisor
  next to the toggle) explains why. Still fully toggleable off for
  anyone who doesn't use either tab and wants to save the disk/bandwidth.

### Added — 👤 face-recognized badge on clip thumbnails

- **New `face_recognized` field on `/api/clips`, and a 👤 badge on the
  clip's thumbnail in the Library grid, when that clip's most recent
  analysis had an approved household member recognized (the same
  face-bypass that clears the suspicious flag)** — previously the only
  way to confirm the bypass had actually fired for a clip was to open
  the Biometrics tab's activity card or check the clip's own AI panel;
  now it's visible at a glance across the whole grid, same as the ★
  starred and 🔔 notified badges. Same "latest analysis wins" scoping as
  the notified badge (`get_clips`'s existing pattern, mirrored for this
  field) — a clip that no longer bypasses after a later re-analysis
  doesn't keep showing the badge forever. Placed just left of the
  selection checkbox rather than overlapping it — that checkbox is
  always present in the grid's top-right corner, not just during
  bulk-select, so it wasn't actually free space.

### Added — biometric accuracy feedback (report a wrong or missed face match)

- **New "Wrong match" / "Report a missed face match" buttons on a clip's AI
  panel, alongside the existing correct/incorrect suspicious-verdict
  feedback** — distinct from that existing feedback (which is about
  whether the suspicious flag itself was right), this is specifically
  about whether face *recognition* got it right. A clip where the
  face-bypass fired shows "Wrong match" (the wrong person was recognized,
  or the bypass shouldn't have applied); a clip where it didn't fire shows
  "Report a missed face match" (an enrolled person was actually present
  but wasn't recognized). Backed by a new `face_recognition_feedback`
  table and `GET`/`POST /api/ai/faces/feedback` endpoints, separate from
  the existing `analysis_feedback` table used for suspicious-verdict
  corrections.
- **Deliberately a pure audit trail, not wired to any automatic threshold
  adjustment** — unlike the suspicious-flag confidence threshold (which
  can only ever get *more* conservative from feedback, see
  `get_effective_confidence_threshold`), automatically loosening
  face-match tolerance from a handful of reports risks the opposite
  mistake: causing the false bypass the safety design explicitly warns
  against (see this repo's CLAUDE.md and `analyzer.py`'s
  `_face_bypass_applies` docstring — a false bypass is a missed genuine
  intrusion, not a cosmetic bug). This data is surfaced for a human to
  review and act on (e.g. re-enrolling someone with clearer reference
  photos), not consumed automatically.
- The Biometrics tab's **Face-bypass activity** card now also lists recent
  reports (type, camera, and time) under a new "🚩 Reported accuracy
  issues" section, so filing a report has a visible place to land instead
  of disappearing into the database.

### Verified — aarch64 release build and runtime behavior

- **`build.yaml`'s aarch64 leg builds and publishes correctly as-is —
  confirmed against this repo's actual GitHub Actions history**, not just
  local reasoning. (An earlier pass through this file briefly added a
  `docker/setup-qemu-action` step in the belief that Buildx can't cross-
  build for a foreign platform without it; that step was reverted after
  checking real run logs — see below.) `docker/build-push-action`'s
  Buildx `docker-container` driver (`moby/buildkit`) carries its own
  build-time multi-platform emulation and has successfully built and
  pushed the `linux/arm64` image for every tagged release back through
  v4.0.2 and earlier, all without a QEMU-setup step. `setup-qemu-action`
  registers emulation for *running* a foreign-platform container
  (`docker run --platform ...`) at the kernel's `binfmt_misc` level — a
  different mechanism from Buildx's own build-time execution, and not
  needed for this workflow, which only ever builds and pushes, never runs
  a foreign-arch container itself.
- **Verified the actual dependency set installs and runs correctly on
  aarch64**: ran a full real (QEMU-emulated) build of this add-on's
  Dockerfile for `linux/arm64` end to end — apt packages,
  `requirements.txt`, and the full optional computer-vision stack
  (`torch`/`torchvision` from PyTorch's CPU wheel index, `ultralytics`,
  `lap`, `opencv-python-headless`, `transformers`, `facenet-pytorch`, and
  `moondream`/`kestrel`) all installed successfully with real prebuilt
  aarch64 wheels — no source compilation, no missing distributions. Then
  actually **ran** the built aarch64 image (not just built it, this time
  via a separately-registered QEMU handler at the container-run level,
  the same mechanism `setup-qemu-action` provides): PostgreSQL 17
  initialized and started correctly on `aarch64-unknown-linux-gnu`, the
  Python app booted, the media server bound to its port and answered
  `/health`, `/`, and `/api/stats` correctly, and a deliberately-invalid
  Blink login was rejected with the same clean error handling (no
  crash-loop) as on amd64.
- No architecture-specific code paths, hardcoded CUDA device selection,
  or arch-conditional file paths were found anywhere in the Python
  backend, rootfs service scripts, or `apparmor.txt` — the one existing
  `platform.machine()` reference (`media_server.py`'s Moondream
  install-status message) is unreachable dead code left over from before
  4.1.0's Debian base-image switch (`_moondream_arch_supported()` always
  returns `True` now) and reflects no live bug.
- On real aarch64 Home Assistant OS hardware (unlike this session's
  amd64 devcontainer), the add-on builds natively with no emulation
  involved at all regardless of the above — `config.yaml` has no
  `image:` field yet, so Supervisor always builds locally rather than
  pulling a prebuilt image (see the earlier "release build/publish
  workflow" review).

### Fixed — tag input, tag filter staleness, and accidental-removal protection

- **Adding a tag with spaces (e.g. "Test tag") silently dropped the
  space** instead of preserving a word boundary (`onTagInputKeydown`'s
  sanitizer stripped anything outside `[a-z0-9_-]` with no substitution
  step first) — "Test tag" became "testtag". Spaces are now converted to
  a single dash before the character filter runs, so "Test tag" becomes
  "test-tag"; multiple/repeated spaces collapse to one dash.
- **A newly-added or just-removed tag didn't appear/disappear from the
  Library tab's tag filter dropdown until a manual page reload.**
  `LibraryPage.vue`'s tag list was fetched once behind a `tagsLoaded`
  guard and never invalidated; `ClipModal.vue`'s tag save never signaled
  anything back to the Library page. Removed the one-time-load guard
  (folded `loadTags()` into the existing `loadAll()`, which the page
  already re-runs on the shared refresh signal) and had `saveTags()`
  bump that shared signal after every add/remove, matching the pattern
  already used for AI feedback (`ClipAiPanel.vue`) — no full-page reload
  needed for either direction.
- **Added a confirmation dialog before removing a tag** (`useConfirm()`,
  the same composable already used for clip deletion and removing an
  enrolled face) — the × was previously a single accidental click away
  from silently removing a tag.

### Verified — thumbnail dependency claim, filter dropdown contrast, and the face-bypass activity card

- **Re-confirmed, against current source, that `VehicleZonePicker.vue`
  is fully `download_thumbnails`-dependent** (both its clip-browsing
  strip and the actual zone-drawing canvas image use `clipThumbUrl`),
  **while `EnrollFromClipPicker.vue` only needs thumbnails for its
  browsing strip** — the actual face-picking step calls a separate
  on-demand endpoint (`GET /api/clips/{id}/frames`) that runs `ffmpeg`
  directly against the clip file, independent of `download_thumbnails`.
  No change from the earlier statement of this — re-verified line by
  line rather than re-asserted from memory.
- **Filter dropdown text contrast in dark mode, checked live in a real
  browser (Playwright) against the running add-on**: every option in
  the Library tab's date-range/source/tag/sort filters, and the
  Biometrics tab's "Show clips from" lookback filter, renders unselected
  options in solid white and the selected option in a tinted purple
  highlight — the fix already shipped earlier in this branch. A reported
  screenshot showing washed-out grey text on these same dropdowns
  reflected a build from before this session's latest redeploy, not the
  current code.
- **The Biometrics tab's Face-bypass activity card is present and wired
  correctly** — it's just showing its empty state, confirmed against the
  live database (0 of 41 analyses this session have `face_bypass_applied
  = true`). This is plausible rather than broken: the bypass only has
  something to clear when the AI's own raw judgment first flags a clip
  suspicious, and re-analyzing recent Front Door clips (including the
  one clip from earlier this session that *was* flagged suspicious) shows
  the AI already judging them non-suspicious on its own — leaving nothing
  for the bypass to override. No evidence of a bug in the bypass logic
  itself was found; if the user wants to force a concrete live example
  from this exact account, that's a further, separate investigation.

### Fixed — AI analysis startup log always showed `model=(auto)` for non-Ollama providers

- **`  AI analysis    : on (provider=..., model=...)` hardcoded `ollama_model`
  regardless of `ai_provider`**, so any non-Ollama provider (openai,
  anthropic, moondream_cloud, ...) always logged `model=(auto)` even with a
  real model configured — found live, testing a real OpenAI setup that had
  `openai_model="gpt-5.4-nano"` explicitly set. `_finish_startup` now maps
  each provider to its own configured model field, matching
  `create_analyzer()`'s own provider dispatch. Covered by two new tests
  (openai and anthropic, to confirm the fix is provider-aware and not just
  an openai-specific patch).

### Added — dedicated Enable Tier-2 Escalation toggle; fixed `ai_escalation_provider` never appearing in Configuration

- **Real, user-facing lockout bug found live-testing this round**:
  `ai_escalation_provider`'s dropdown never appeared in HA Supervisor's
  Configuration tab on a fresh install. Root cause confirmed by direct
  testing against a live Supervisor instance (schema, validation, and
  persistence all worked correctly via the Supervisor API) — the field was
  deliberately optional with no default, to avoid a startup-validation
  failure a stored `""` causes for an optional enum, but HA Supervisor's
  Configuration UI never renders a row for an optional select field that
  has no value in options.json. Every fresh install starts in exactly that
  state, so no one could ever turn escalation on through the UI alone.
- **Fixed properly rather than patched around**: added a new **Enable
  Tier-2 Escalation** toggle (`ai_escalation_enabled`, off by default) as
  the real on/off switch. `ai_escalation_provider` is now a required field
  with a real default (`"ollama"`) — always has a concrete value for its
  dropdown to show — and is only consulted when the toggle is on;
  `_resolve_ai_escalation()` forces it back to disabled regardless of
  whatever provider is selected when the toggle is off. The existing
  `openai_escalation_model` legacy-migration path (for installs from before
  two-tier escalation existed) now also implicitly turns the new toggle on,
  so upgrading installs relying on it keep working unchanged.
- **Verified all downstream consumers needed zero changes** — `create_analyzer()`'s
  escalation wiring, `media_server.py`'s status API, and the AI tab's tier-2
  display all key off the already-resolved provider string, which stays the
  empty-string "disabled" contract they already expected.
- **Filled a real test-coverage gap found while auditing this**: no existing
  test combined face-recognition bypass with escalation, despite that being
  a safety-critical interaction (see this file's face-bypass section).
  Added coverage confirming the bypass correctly overrides an *escalated*
  (tier-2) suspicious verdict when every face in the clip is an approved
  household member, and that the adversarial "an approved member and a
  stranger both present" case still stays suspicious through the
  escalation path too — the bypass must never regress here, escalated or
  not.
- Added missing translations for `ai_escalation_provider`, `ai_escalation_model`,
  `ai_escalation_enabled`, `ai_prompt_debug_enabled`, and
  `moondream_finetune_model` — all previously rendered as raw snake_case
  option names with no description in the Configuration tab. Removed the
  stale `openai_escalation_model` translation entry left over from when
  that option itself was already removed from `config.yaml`'s schema in an
  earlier round.

### Added — automatic CPU-compatibility guard for the computer-vision pipeline

- **Real risk found researching aarch64/Raspberry Pi deployment**: PyTorch's
  official ARM builds assume the CPU supports the ARMv8.1 LSE atomic
  instructions. Raspberry Pi 4 and older boards (Cortex-A72 and earlier)
  don't have them, and this has caused long-standing, still-unresolved
  upstream `illegal instruction` crashes across many PyTorch versions
  (e.g. [pytorch/pytorch#176993](https://github.com/pytorch/pytorch/issues/176993)).
  Critically, this is a hardware-level signal (SIGILL), not a Python
  exception — nothing in this add-on's existing "report unavailable rather
  than raise" pattern for missing dependencies could have caught it, since
  that pattern only guards against `ImportError`, and the crash happens
  *during* the import itself. Raspberry Pi 5's Cortex-A76 is unaffected.
- `vision.py` now checks CPU compatibility (reading `/proc/cpuinfo` for the
  `atomics` feature flag, always true on non-ARM) *before* every
  torch-dependent stage's import — object detection, depth estimation,
  contact segmentation, and local face recognition — via a new
  `torch_cpu_compatible()` check and a dedicated `CPUIncompatibleError`
  raised early enough to prevent the risky import from ever executing.
  Each stage reports itself unavailable (a clean warning, no crash) exactly
  like the existing missing-package case, distinct from an unrelated
  `RuntimeError` during model loading so that failure mode keeps its own
  full-traceback logging. `is_face_recognition_available()` — already
  surfaced to the web UI and gating the face-enrollment endpoint — now
  factors this in too, and the general AI status API exposes a new
  `torch_cpu_compatible` field for the frontend to use going forward.
- Updated README.md and DOCS.md to describe this as automatic protection
  rather than a "don't enable this" warning users would have to remember.
- **Biometrics nav tab now hides itself** when face recognition is
  unavailable (incompatible CPU or missing dependencies) rather than
  sending users into a tab where every action would fail — new
  `useCapabilitiesStore`, checked once by `AppSidebar` via the existing
  `GET /api/ai/faces` `available` field. Verified the **Vehicles** tab
  (including the car-zone rectangle picker) needs no equivalent change —
  it's built entirely on frame-diffing and JSON storage, no PyTorch
  dependency at all, so it already works identically regardless of CPU
  compatibility.
- **CI now builds and smoke-tests on real aarch64, not just amd64** —
  `build` and `smoke-test` in `ci.yaml` are now a 2-way matrix using
  GitHub's native arm64-hosted runners (`ubuntu-24.04-arm`, free for public
  repos), so both jobs run natively on each architecture with no QEMU
  involved for either building or running. This is what actually would
  have caught the Pi 4-class illegal-instruction risk in CI going forward,
  rather than relying on this round's one-off manual verification.

### Added — persisted vehicle-zone reference image, Clear confirmation, and freeform (lasso) zone drawing

- **The car-zone picker's reference frame is now persisted at save time**,
  instead of always showing whichever clip happens to be newest whenever
  the Vehicles tab is revisited. Saving a zone captures a still snapshot of
  the exact frame it was drawn on (served from a new
  `/data/vehicle_zone_snapshots/<camera>.jpg`, one per camera) and shows
  that frozen image with the zone overlaid from then on — so something
  that shows up in a *later* clip (another car, a person) never visually
  overlaps a zone that was set before it arrived, even though the
  underlying saved zone data was never actually affected by this in the
  first place.
- The picker is now a **preview/edit** flow rather than always-interactive:
  preview shows the persisted snapshot + zone (read-only) with **Edit
  zone**/**Clear zone** actions; edit shows the interactive thumb-strip +
  drawing canvas, entered explicitly, with nothing persisted until **Save
  zone** is clicked (drawing a rectangle no longer silently auto-commits on
  every drag release the way it used to).
- **Clear zone** now asks for confirmation (the existing app-wide confirm
  dialog) before removing a saved zone and its reference image, landing on
  a clear empty state ("No vehicle selected. Select your vehicle in a
  frame below and click save to set a vehicle.") rather than an ambiguous
  blank picker.
- **New freeform ("lasso") drawing mode**, alongside the existing
  rectangle tool, via a PrimeVue `SelectButton` mode switch — click, hold,
  and trace an outline around the vehicle; releasing the pointer anywhere
  auto-closes the shape back to the start point, matching a real paint
  app's lasso tool rather than requiring a precise manual trace back to
  where you started. Useful for a vehicle a rectangle would necessarily
  over-include a lot around (parked at an angle, boxed in by neighbors).
- `car_zone` is now a shape-discriminated value (`{"shape": "rect", ...}`
  or `{"shape": "polygon", "points": [...]}`) end to end —
  `_normalize_car_zone` (backend validation), the frontend `CarZone` type,
  and `_zone_motion_fraction`'s AI-prompt "zone motion" signal (true
  point-in-polygon matching for freeform zones, not just a bounding-box
  approximation) all understand both shapes. Zones saved before this
  feature existed have no `shape` key at all and are treated as
  rectangles — no data migration needed. The Moondream fallback proximity
  hint (used when a clip's own car detection finds nothing) uses a new
  `_car_zone_bbox` helper to reduce either shape to a plain bounding box,
  since that one code path is already just an approximation, not exact
  geometry.
- New dedicated endpoints — `PUT`/`DELETE /api/vehicle/zone/{camera}`,
  `GET /api/vehicle/zone-snapshot/{camera}` — so saving or clearing a zone
  takes effect immediately, the same way the rest of the Vehicles tab's
  camera settings already do, rather than staying implicit in the
  page-level "Save Camera Settings" batch action (which still round-trips
  `car_zone` unchanged, so nothing about that existing flow broke). Saving
  a zone now also force-sets `is_car_camera: true` on that camera's config,
  since the picker is only reachable once that flag is on but it's
  normally only persisted by the separate batch save — without this, a
  zone saved before ever clicking that batch save would have silently had
  no effect.

### Testing — coverage pushed further toward 100%

- Backend now at 99.96% (`analyzer.py`/`database.py` each have exactly one
  remaining line, both confirmed structurally unreachable rather than
  untested: `_clean_summary`'s dedup loop can't produce an empty split
  segment once `text.strip()` has already run, and `get_feedback_stats`'s
  `not row` guard can't fire because a bare aggregate query with no
  `GROUP BY` always returns exactly one row in PostgreSQL, even over zero
  matching rows). Closed real gaps along the way, including a genuine
  async double-checked-locking race in `MoondreamLocalAnalyzer._ensure_model`
  (a second caller blocked on the load lock must see the model already
  ready once it acquires it, not reload it) and several `self._pool is
  None` "not connected yet" guards in `database.py` that had no direct
  test.
- Frontend now at 99.16% statements / 99.84% lines (up from 98.69% /
  99.47%), including closing every reachable gap the new vehicle-zone-picker
  code introduced. Remaining gaps are almost entirely branch-only (both
  sides of a conditional are already exercised by different tests; V8's
  branch counter just wants every logical-operator permutation individually)
  spread thinly across many files — chasing those further has real
  diminishing returns against the 80% CI gate both suites already clear
  comfortably.

### Testing — CI smoke test now verifies real tab content, not just clicks

- `e2e/smoke.mjs` previously clicked through every nav tab and only checked
  for console/page errors — a tab stuck on its loading spinner forever, or
  one that silently rendered nothing, passed the smoke test as long as
  nothing *threw*. It now waits (with a real, failing timeout, not a fixed
  sleep) for a tab-specific piece of text that only appears once that
  page's own component has actually mounted and its data fetch has
  resolved into its real UI — e.g. Library's "No clips found" empty state,
  the AI tab's "AI Analysis Not Configured" card, Vehicles' "No cameras
  found" message — scoped to that tab's own `#page-<name>` container so it
  can't accidentally match a different, inactive tab's still-mounted (but
  CSS-hidden) DOM. Also confirms each tab's loading spinner is actually
  gone once its content renders, catching a stuck/partial render that a
  content-only check would miss. Verified against a real locally-built
  image before landing this — every assertion passed on the first run.
- `scripts/smoke-test.sh`'s curl/jq endpoint checks had drifted out of sync
  with `ci.yaml`'s inline copy (missing the `/api/vehicle/settings` and
  `/api/ai/camera-configs` checks CI already had) despite both claiming to
  be "one source of truth" — resynced.

### Added — new CI job installs the add-on through a real HA Supervisor instance

- New `supervisor-install-test` job in `ci.yaml`, independent of and
  running in parallel with `build`/`smoke-test`. Where the existing
  smoke-test boots the add-on via a bare `docker run` (fast, but bypasses
  Supervisor entirely), this job runs the same `ghcr.io/home-assistant/
  devcontainer:5-apps` Supervisor-in-Docker environment this add-on has
  been manually verified against all through development: bootstraps a
  real Supervisor, copies the checked-out source into its local-apps
  directory, and lets Supervisor itself discover, build, and install the
  add-on from source — the same path a real HAOS install takes, since
  `config.yaml` has no `image:` field. Catches the class of bug a bare
  container can't see at all: config.yaml schema validation, AppArmor
  profile registration, and whatever the generated Configuration tab
  actually renders — not hypothetical, this is exactly the shape of bug an
  earlier round of this same testing session found by hand (an
  optional-select schema quirk that permanently hid a Configuration field
  in the real Supervisor UI while every container-level check stayed
  green).
- Every command in the new job was verified against a real, disposable
  Supervisor instance before landing (not just written from documentation)
  — including confirming the actual CLI verb for a *fresh* local-add-on
  install (`ha refresh-updates` to make Supervisor discover it, then
  `ha apps install`, which does not auto-start — a separate `ha apps
  start` is required), which this project had never exercised before
  (every prior manual round only ever rebuilt an add-on already installed
  from an earlier session). A real from-scratch build (no Docker layer
  cache at all, full CV-pipeline dependencies included) took about 6.5
  minutes; the job's 20-minute timeout leaves comfortable headroom above
  that plus Supervisor's own bootstrap.
- Found one real, pre-existing issue while verifying this: Supervisor logs
  `App local_blink_clip_downloader uses build.yaml which is deprecated.
  Move build parameters into the Dockerfile directly` on every install —
  not fixed here (out of scope for a CI-only change), flagged for a
  follow-up.
- The job's first real runs (both on GitHub Actions and under local `act`)
  failed before ever reaching the add-on itself, exposing three bugs the
  earlier by-hand command verification hadn't caught because it never ran
  the assembled script back-to-back on a cold environment:
  - The devcontainer and its three volumes were named identically to this
    project's own long-running local Supervisor dev environment
    (`supervisor-mnt`/`supervisor-docker-lib`/`supervisor-containerd-lib`).
    On a machine where that dev environment is already running, the job's
    `docker run` silently attached to those in-use volumes instead of
    getting fresh ones, and its cleanup step could have removed them out
    from under the dev environment. Renamed to a `ci-supervisor-*` prefix,
    with defensive pre-removal before creating them, so the job is safe to
    run repeatedly alongside that dev environment without ever touching it.
  - `supervisor_run` is started via a detached `docker exec -d` so the step
    can return immediately, but that means its stdout/stderr weren't the
    container's own entrypoint output — `docker logs` on failure showed
    nothing useful. Redirected to a file inside the container that the
    failure branch now dumps.
  - Two separate readiness races: the job could invoke `supervisor_run`
    before the container's own nested Docker daemon had finished starting,
    and — less obviously — `hassio_cli` existing is not the same as
    Supervisor's own internal state machine reaching `running`; commands
    issued in between fail with `System is not ready with state: setup`.
    Both are now polled for explicitly before moving on. The second poll
    initially looked broken even after this fix (a real `act` run failed
    in ~30s, far short of its own timeout) — the actual bug was one level
    down: `ha info` itself exits non-zero while not ready, and capturing
    it into a variable first (`state_line=$(...)`) before testing it is an
    ordinary assignment statement, not the direct condition of an `if`, so
    it wasn't exempt from the workflow runner's `bash -e` and killed the
    step on the very first "not ready" check instead of retrying.
  - Verified with a full local `act` run reaching `Job succeeded`
    end-to-end: Supervisor bootstrap, add-on discovery, a from-scratch
    install, start, and the media-server health check all passed.
  - That local pass didn't catch everything: the same fixes still failed
    on a real GitHub Actions run, one step later this time — the add-on
    itself reached Supervisor's own `state: started` promptly, but the
    following health-check step exhausted a 10-attempt/30s budget with
    *every* attempt failing outright (not "succeeded on a late attempt").
    Root cause: Supervisor's "started" bookkeeping only means the
    container process was launched, not that the app inside has finished
    booting — this add-on's entrypoint initializes a brand new PostgreSQL
    17 data directory from scratch before the Python app can even connect,
    let alone bind its port, and a real GitHub-hosted runner is slower and
    more contended than the local machine this was developed against
    (where the same check had passed in well under a second). Both the
    add-on-start check and the health-check loop are now more generous,
    and re-verified via another full local `act` run — which itself this
    time took over twice as long to reach `state: running` as the previous
    local run (system load varies run to run), a real illustration of why
    a tight, "worked on my machine" timeout isn't a safe bet for a shared
    CI runner.
  - A third real GitHub Actions run, after that timing fix landed, failed
    differently again: the media-server health check exhausted its full
    (now 40-attempt/120s) budget with *every single attempt* failing, not
    a late-attempt near-miss — ruling out "just needs more time" entirely.
    The dumped `hassio_supervisor` log (not the add-on's own log) showed
    the real cause: a wall of `s6-ipcserver-socketbinder: fatal: unable to
    create socket: Permission denied`, an internal control socket
    Supervisor's own container was stuck permanently retrying and never
    winning. Root cause: GitHub's Ubuntu runner images run with AppArmor
    active by default, unlike the host this job was developed and locally
    verified against via `act` (`/sys/module/apparmor/parameters/enabled`
    reads `N` there — the kernel has no AppArmor enforcement to trigger in
    the first place). `--privileged` on this job's own outer
    `ha-supervisor-ci` container doesn't make its *inner* dockerd's own
    default container security profile unconfined for the containers *it*
    launches (`hassio_supervisor`, two levels of nesting deep) — on a host
    where AppArmor is actually enforcing, that inner container hit a
    real confinement conflict. This is an acknowledged, known class of
    issue for nested/privileged containers on GitHub-hosted runners
    specifically (`actions/runner-images#10015`: "apparmor should be
    disabled by default on Ubuntu" — these are single-job ephemeral VMs
    with full root access already, so AppArmor there has no real security
    benefit, only the cost of exactly this kind of failure). Added a
    GitHub-Actions-only step (`aa-teardown`, falling back to `systemctl
    stop apparmor`) immediately after checkout to unload it before the
    Supervisor devcontainer starts. **Could not be verified locally** —
    the whole point of the fix is a host difference `act`/this dev
    machine structurally cannot reproduce (confirmed: the new step
    correctly no-ops there, and the rest of the job still passes) — this
    one needs a real GitHub Actions run to confirm either way.
  - **That AppArmor fix did not work.** A real GitHub Actions run showed
    the new `aa-teardown` step executing successfully ("Unloading AppArmor
    profiles") and the exact same `s6-ipcserver-socketbinder: Permission
    denied` wall still appearing — direct evidence AppArmor enforcement
    wasn't the (whole) cause, since tearing it down host-wide before the
    nested container even exists should have prevented it entirely if it
    were. Re-reading the fuller log changed the diagnosis in a second way
    too: Supervisor's own bookkeeping (`ha apps info` → `state: started`)
    reported success even though `docker logs
    addon_local_blink_clip_downloader` came back **completely empty** —
    not slow to populate, empty, meaning the container never actually ran
    at all. That means the socket errors were the real blocker after all,
    just not for the reason first suspected. Root cause, on a second,
    more targeted search: Docker 20.10+ defaults new containers to a
    *private* cgroup namespace on a cgroup v2 host, which breaks nested
    Docker-in-Docker tooling like this whose inner dockerd needs to
    manage its own containers' cgroups the way a *host* cgroup namespace
    allows — `--privileged` alone does not change this. Confirmed this
    dev machine's own WSL2 Docker already behaves like `--cgroupns=host`
    by default regardless of flags (a fresh container's `/proc/self/cgroup`
    shows a flat `0::/`, not the nested `0::/docker/<id>` path a real
    private cgroup namespace produces) — direct evidence for *why* this
    class of bug specifically cannot reproduce locally on this machine,
    on top of the general "GitHub's runner differs from this dev
    machine" pattern the AppArmor entry above already covers. Added
    `--cgroupns=host` to the outer `ha-supervisor-ci` container's
    `docker run`, unconditionally (harmless on a host where it isn't
    needed, unlike the AppArmor step there's no reason to gate it behind
    `!env.ACT`). Sanity-checked locally (still passes), but — same
    caveat as the AppArmor attempt — the actual fix can only be confirmed
    by a real GitHub Actions run.
  - **That cgroup fix didn't resolve it either** — a real GitHub Actions
    run showed the exact same `s6-ipcserver-socketbinder: Permission
    denied` wall, unchanged. Found the actual missing piece by reading the
    [home-assistant/devcontainer](https://github.com/home-assistant/devcontainer)
    project's own README rather than general Docker/kernel research: this
    devcontainer image has its own internal AppArmor handling, independent
    of anything the outer job does at the runner-host level — "If the host
    kernel supports AppArmor, it is automatically active inside the
    devcontainer for the Supervisor and apps," loading its own
    `hassio-supervisor` profile. The earlier `aa-teardown` step tore down
    AppArmor on the *outer* GitHub runner host before `ha-supervisor-ci`
    even existed, but that doesn't stop the devcontainer's own entrypoint
    from separately detecting kernel-level AppArmor support and re-loading
    its own profile regardless — host-level teardown and the
    devcontainer's own internal activation are independent mechanisms.
    `SUPERVISOR_UNCONFINED=1` is that project's own documented environment
    variable for making the Supervisor container run `apparmor=unconfined`
    instead (normally set via `containerEnv` in a `devcontainer.json`,
    which is just a plain container environment variable, so `-e` on a
    bare `docker run` does the same thing). Added it alongside the
    existing `--cgroupns=host` and `aa-teardown` fixes, all three kept
    together since each targets a distinct, independently-confirmed
    mechanism. Worth noting this may only cover Supervisor's *own*
    confinement — `hassio_supervisor`'s log separately showed
    `[supervisor.host.apparmor] Adding/updating AppArmor profile:
    local_blink_clip_downloader`, a **second**, per-add-on AppArmor
    profile Supervisor generates dynamically from the add-on's own
    `apparmor.txt`, which `SUPERVISOR_UNCONFINED` doesn't obviously
    address — flagged as a likely next thing to investigate if this round
    doesn't fully resolve it either. Sanity-checked locally (still
    passes), same caveat as both previous attempts — only a real GitHub
    Actions run can confirm whether it actually works.
  - **Confirmed: that flagged per-add-on profile was the actual cause all
    along.** A real GitHub Actions run finally produced the *complete*
    diagnostic dump (earlier rounds had been working from a truncated
    view). It showed the socket-permission wall starting the instant
    Supervisor logged `Starting Docker app
    local/amd64-addon-blink_clip_downloader`, interleaved with that
    add-on's own s6-overlay's first-ever boot line (`s6-rc: info: service
    s6rc-oneshot-runner: starting`) — meaning this was **the add-on's own
    container failing to boot**, the whole time, not `hassio_supervisor`
    itself. It only ever *looked* like Supervisor's own problem because
    Supervisor's docker manager attaches to a freshly-started add-on and
    folds its output into `docker logs hassio_supervisor`, while `docker
    logs addon_local_blink_clip_downloader` directly came back empty
    every round (the add-on container's own log capture apparently never
    got far enough to attach before this crashed it). Retroactively,
    every fix so far had been aimed at the wrong container: `aa-teardown`
    and `SUPERVISOR_UNCONFINED` both only affect Supervisor's *own*
    confinement, and `--cgroupns=host` targets the outer job container —
    none of them touch the **separate** AppArmor profile Supervisor
    generates and loads per add-on (`local_blink_clip_downloader` here,
    confirmed via `[supervisor.host.apparmor] Adding/updating AppArmor
    profile: local_blink_clip_downloader` in the log), which is what
    actually confines the add-on's container. There's no documented
    per-add-on equivalent of `SUPERVISOR_UNCONFINED`, and this add-on's
    own `config.yaml` is deliberately *not* being changed to opt out —
    that's a real security boundary for actual end-user installs, not a
    knob worth trading away just to unblock a CI-only nested-container
    artifact. Instead, mask AppArmor's kernel interface (`securityfs` at
    `/sys/kernel/security/apparmor`) from everything inside the outer
    `ha-supervisor-ci` container before Supervisor ever starts (a tmpfs
    mounted over it, only if present), so Supervisor's own
    host-capability detection correctly concludes AppArmor isn't usable
    at all in this environment — uniformly, for itself and for every
    add-on it manages — without touching anything the add-on ships or how
    a real install behaves. Also added `--security-opt apparmor=unconfined`
    explicitly to the outer container (`--privileged` should already
    imply this, but costs nothing to state directly). Kept all of the
    previous fixes in place alongside these two — cumulatively addressing
    every AppArmor/cgroup-namespace mechanism found across this whole
    investigation, not replacing earlier attempts. Sanity-checked locally,
    though this dev machine has no `/sys/kernel/security/apparmor` at all
    to mask in the first place, so — more than any fix so far in this
    thread — only a real GitHub Actions run can actually confirm this one.
  - **That fix worked for the add-on's own boot failure — the
    `s6-ipcserver-socketbinder` wall is gone entirely — but introduced a
    new regression in the process.** Masking only the `apparmor/`
    subdirectory left it existing-but-empty, and Supervisor's own
    plugin-launch code (used for *every* container it starts, not just
    the add-on) separately does `open(/sys/kernel/security/apparmor/
    profiles)` to check whether a specific profile is loaded, hard-erroring
    on a plain "file not found" instead of treating AppArmor-unavailable
    as the graceful fallback — a real run's error, verbatim: `Could not
    check if docker-default AppArmor profile was loaded: open
    /sys/kernel/security/apparmor/profiles: no such file or directory`.
    That blocked *every* plugin container (`hassio_dns`/`hassio_audio`/
    `hassio_multicast`/`hassio_observer`, all stuck in `Created`, never
    started) — worse than the original symptom, since Supervisor never
    even reached `hassio_cli` this time. An empty-but-present `apparmor/`
    directory is an unusual, thinly-tested state; genuinely having no
    AppArmor kernel support at all (the parent securityfs path itself
    absent) is the far more common, robustly-handled real-world case that
    Supervisor's own code is much more likely to have been tested
    against. Changed the mask to cover the *parent* (`/sys/kernel/
    security`) instead of just the `apparmor/` subdirectory, so the whole
    path — and everything under it — genuinely doesn't exist rather than
    existing hollow. Confirmed locally this path is already empty on this
    dev machine (so masking it is a true no-op there, same verification
    limit as the previous attempt) before landing.
  - **That theory was wrong too — proven by the identical error recurring
    byte-for-byte on the next real run.** `open()` raises the same "no
    such file or directory" whether the immediate file or a parent
    directory is missing, so a recurring identical error message doesn't
    distinguish those cases — the real signal was that masking the parent
    made *no observable difference at all*. That means Supervisor's
    plugin-launch code has no graceful fallback for this specific check
    at all (unlike its general AppArmor capability detection elsewhere,
    which does degrade gracefully): it unconditionally tries to open
    exactly `/sys/kernel/security/apparmor/profiles` whenever starting
    any container, and treats any failure to open it as fatal, regardless
    of the reason. The only way to satisfy that is to make the file
    genuinely exist and be readable — empty is fine, since empty
    correctly means "no profiles currently loaded" — not to make the
    surrounding path vanish more thoroughly, which was exactly backwards.
    Reverted to masking just the `apparmor/` subdirectory (which is what
    actually fixed the add-on's own boot failure) and added `touch
    /sys/kernel/security/apparmor/profiles` inside it, so the specific
    `open()` call Supervisor's own error message named succeeds.
  - **That fix worked for its own specific check, and moved the failure
    one level deeper again — which turned out to be the actual dead end.**
    A real run confirmed the "check if loaded" error was gone, replaced
    by a new one: `AppArmor enabled on system but the docker-default
    profile could not be loaded: running '/usr/sbin/apparmor_parser -Kr'
    failed with output: Cache read/write disabled: interface file
    missing.` Every fix up to this point worked by making a userspace
    file *read* succeed (does a path exist; does opening a specific file
    return sensible content) — this one is fundamentally different:
    `apparmor_parser` needs to actually **load a profile into the
    kernel's real AppArmor LSM**, a genuine kernel operation that no
    amount of tmpfs file-stubbing from inside a container can provide,
    since the kernel itself is the only thing that can process it. Nine
    rounds of real GitHub Actions failures (see the full account above)
    fixed every layer that could be faked from userspace; this is the
    layer that can't be. Removed the `supervisor-install-test` job
    entirely rather than continue — the entries above are kept as-is,
    unedited, as a record of what was tried and why, in case a future
    attempt at Supervisor-based CI testing for this add-on wants to pick
    up from here instead of rediscovering the same nine layers.

### Fixed — `build.yaml` deprecation warning

- Supervisor logged `App local_blink_clip_downloader uses build.yaml which
  is deprecated. Move build parameters into the Dockerfile directly` on
  every install (first noticed while verifying the CI job above). As of
  Supervisor 2026.04.0, `BUILD_FROM` is no longer auto-provided from
  `build.yaml`'s per-arch `build_from` mapping; the [official migration
  guide](https://developers.home-assistant.io/blog/2026/04/02/builder-migration/)
  recommends a single hardcoded `FROM` in the Dockerfile instead. Deleted
  `build.yaml` and changed the Dockerfile's `FROM ${BUILD_FROM}` (with a
  per-arch `ARG BUILD_FROM`) to a single `FROM
  ghcr.io/home-assistant/base-debian:trixie` — confirmed via `docker
  buildx imagetools inspect` that this is a real multi-platform manifest
  covering both `linux/amd64` and `linux/arm64` under one tag, not a
  per-arch image, so BuildKit resolves the right platform automatically
  from whatever `--platform`/`platforms` the build is invoked with.
  `BUILD_ARCH` is unaffected (the migration guide keeps it; still used for
  the `io.hass.arch` OCI label) and both this repo's CI workflows already
  drove target architecture via their own `--platform`/`platforms` inputs
  rather than reading `build.yaml`, so `.github/workflows/build.yaml` and
  `ci.yaml`'s `build` job needed only their now-unnecessary `base_image`/
  `build_from` matrix fields and `BUILD_FROM=` build-args removed, no
  structural changes.
- Verified with real, from-scratch builds on both architectures (not just
  reading the manifest) before landing: amd64 built and ran natively;
  aarch64 built and ran under QEMU emulation, including the full add-on
  entrypoint (s6-overlay init, a real PostgreSQL 17 bootstrap, the app
  starting in web-only mode without `/data/options.json`, then a graceful
  shutdown) and confirming every optional computer-vision dependency
  (`torch`, `torchvision`, `ultralytics`, `facenet-pytorch`, `opencv`)
  still imports cleanly on aarch64.

### Verified — final pre-release pass against the real live account

Before this release: a full automated re-run (ruff format/check, pyright,
pytest, eslint, prettier, vue-tsc, vitest) confirmed no regressions from
everything landed this cycle (backend 99.96% coverage, frontend 99.16%
statements, 568 frontend tests), and a real browser testing pass against
the live devcontainer (real Blink account, real OpenAI credentials)
covered every nav tab, the vehicle-zone-picker end to end (viewing an
already-saved zone, entering edit mode, drawing a rectangle, canceling
without saving, and the clear-zone confirmation dialog — all confirmed to
persist nothing unless explicitly saved), a mobile viewport pass (no
horizontal overflow), and dark mode.

- **Added — graceful fallback when a clip's video can't be played.**
  `ClipModal.vue` previously let Video.js's own raw, unstyled "The media
  could not be loaded…" browser error render directly inside the player
  chrome on any playback failure. It now shows the clip's own thumbnail
  with a plain-language message and a "Download instead" link, matching
  the rest of the UI, and resets cleanly when navigating to another clip
  instead of carrying a stale error over. **Important correction to how
  this was found**: initial browser testing this session showed *every*
  clip failing this way, which looked like a serious, widespread codec
  bug — it wasn't. That testing used Playwright's bundled open-source
  Chromium, which (unlike real Chrome, Edge, Safari, or Firefox) lacks
  licensed H.264 decoding by design; re-testing the identical clips,
  through the real app UI, with real Google Chrome confirmed every one
  plays correctly, including the original one that looked broken.
  Verified with `ffprobe` too: the files themselves are standard,
  well-formed H.264 (`ffprobe` reports `probe_score=100`). Kept the
  fallback anyway as a genuine hardening improvement for any real future
  playback failure (a corrupted download, a truly unsupported edge case)
  rather than reverting it — it does not trigger for clips that actually
  play, confirmed via the same real-Chrome re-test.
- **Flagged, not changed**: this file's own `## 5.0.0` section has grown
  very long (180+ subsections) tracking the full development/debugging
  history rather than reading as end-user-facing release notes. Worth a
  developer decision — condense for the actual release announcement, or
  keep as-is and write user-facing notes separately — before publishing,
  not resolved here.

### Fixed — 👤 face-recognized badge almost never showed, even when recognition succeeded

- **Reported against a real clip** (a Front Door recording where the
  household member was looking directly at the camera) as "facial
  recognition doesn't appear to be working." Reproduced with a direct,
  targeted diagnostic rather than reasoning from the code alone: extracted
  the exact frames `analyzer.py` would have sampled from that clip via the
  same `ffmpeg` command it uses, ran the same MTCNN + InceptionResnetV1
  models against them, and compared the result to the person's real
  enrolled embeddings pulled from the live database. Recognition had
  actually succeeded — one sampled frame matched an enrolled embedding at
  0.8075 cosine similarity, comfortably above the 0.75 match threshold.
  The bug was never detection accuracy; it was that the Library's only
  "recognized" signal (`face_recognized` on `GET /api/clips`) was derived
  from `face_bypass_applied`, which only ever gets set when a clip is
  *also* suspicious (see `_face_bypass_applies`, gated behind
  `if is_suspicious and ...`). The ordinary case — a household member's
  own routine visit, which the AI never called suspicious in the first
  place — never reaches that gate at all, so the badge essentially never
  had a chance to show for the case it exists to cover.
  Fixed by adding a new field, `approved_faces_seen`, computed from the
  exact same all-or-nothing recognition condition `_face_bypass_applies`
  already checks (at least one approved match, zero unrecognized or
  unapproved faces anywhere in the clip's sampled frames) but recorded
  unconditionally rather than only when the bypass fires. `get_clips`'
  `face_recognized` now reads this new field instead of
  `face_bypass_applied`. The safety-critical bypass logic itself —
  `_face_bypass_applies`, and the suspicious-flag clearing it gates — is
  untouched; `approved_faces_seen` is purely an additional, informational
  read of the same already-computed result. The Biometrics tab's bypass
  audit card (`get_face_bypass_stats`) also keeps querying
  `face_bypass_applied` directly, so it still reports only genuine
  suspicious-flag-clearing events, not every routine recognized visit.
  New `analysis_results.approved_faces_seen` column via a migration
  (existing rows default `FALSE` until their next re-analysis).
- **Second, deeper bug found re-testing the same clip live**: re-analyzing
  the exact reported clip after the fix above still returned
  `approved_faces_seen: false`. Diagnosed directly against the real
  pipeline (not a re-implementation): `extract_frames()` pulls a generously
  oversampled raw pool (14 frames for this clip), but
  `_downselect_frames()` immediately trims that down to just the handful
  actually sent to the AI (5 by default), chosen by *motion* — entry frame,
  peak-motion frame, exit frame, spread across the timeline. Face
  recognition was running against that already-trimmed set, not the raw
  pool. A person standing still to look directly at the camera is exactly
  a *low*-motion moment, so this clip's one clean, front-on, high-confidence
  frame (MTCNN detection probability 0.9999) was consistently the frame
  motion-scoring dropped — confirmed by literally calling the real
  `_downselect_frames()` against the real raw pool and checking whether
  that frame's index survived (it didn't). Fixed by threading the raw,
  pre-down-selection pool through to face recognition specifically
  (`analyzer.py`'s new `_apply_vision_pipeline(..., raw_frames=...)` /
  `vision.py`'s new `VisionPipeline.process_clip(..., face_recognition_frames=...)`
  parameters), while every other stage — object detection, depth, contact,
  and what actually gets sent to the AI model — keeps using the same
  down-selected set as before, unchanged. This is a strict widening of the
  data face recognition gets to look at against the same, unmodified
  matching logic; it cannot make a real stranger *less* likely to be
  caught, only make a real household member *more* likely to be found.
- **Flagged, not changed — a third, deeper layer under the two fixes
  above.** Re-running the real `FaceRecognizer.recognize()` against the
  widened raw pool for this same clip does now find the approved match
  (`approved_names=["Brian"]`) — but `unrecognized_present` is *also* still
  `True`, so `_face_bypass_applies`'s all-or-nothing condition still isn't
  met and `approved_faces_seen` stays `False` for this specific clip.
  Root cause: `recognize()` scores every detected face independently, and
  as the same real person walks toward a camera their face is naturally
  captured at a range of quality — a few frames of this clip detected a
  face that scored well below the 0.75 match threshold (similarity
  0.27-0.43) against the enrolled reference photos, most likely motion
  blur or an off angle mid-stride, not a second person. The current logic
  has no way to distinguish "the same approved person at a bad angle"
  from "an actual stranger" — it treats any non-matching detection as
  the latter, by design (`_face_bypass_applies`'s docstring: "a single
  stranger... must still allow the clip to be flagged; this is a hard
  safety requirement, not a convenience default, so do not loosen this
  condition without equally strong justification"). Deliberately **not**
  changed here: distinguishing the two cases correctly needs real
  cross-frame identity tracking (e.g. clustering a low-confidence
  detection with a high-confidence approved match in an adjacent frame by
  spatial continuity) — meaningfully more surface area in exactly the code
  this repo's own CLAUDE.md singles out as safety-critical, and a genuine
  false-negative-rate trade-off on a security feature that deserves a
  deliberate decision rather than a same-session judgment call. Net effect
  today: recognition is now substantially more likely to succeed than
  before this round of fixes (any clip where the person is clearly visible
  in *any* extracted frame, without a lower-quality detection elsewhere,
  now works), but a clip where the same person is also caught at a poor
  angle elsewhere can still fail to show the badge / fire the bypass —
  the system stays conservative (keeps the suspicious flag, if any) rather
  than risking a false clear.

### Added — recognized clips name the household member instead of "a person"

- **AI analysis summaries now name an approved household member whenever
  one was recognized, even on an already-not-suspicious clip** — "Brian
  walked up the driveway" instead of "A person walked up the driveway".
  Previously `_personalize_summary` only ran inside the safety-bypass
  branch (`if is_suspicious and bypass_condition_met`), so a clip the AI
  never called suspicious in the first place — the overwhelmingly common
  case — kept the generic wording despite recognition having succeeded.
  Restructured `_analyze_clip_locked` so personalization runs whenever the
  same all-or-nothing recognition condition is met, independent of
  `is_suspicious`; clearing the suspicious flag itself (`face_bypass_applied`,
  the actual safety bypass) stays exactly as gated as before. No change to
  `_face_bypass_applies` or any safety-critical logic — this only affects
  which text a summary the AI already wrote gets rewritten with, entirely
  locally, same as the existing bypass-path personalization always did.
- **New 👤 Recognized stat** on the Library's stats row, alongside Today/
  This week/Total/★ Starred — a live count of non-archived clips whose
  latest analysis has `approved_faces_seen`, same semantics and latest-row
  scoping as the `face_recognized` column `get_clips` already returns.
- **Face-recognized badge moved to the thumbnail's bottom-right corner**,
  now the only badge there — previously top-right, squeezed to the left of
  the always-present selection checkbox. Also removed the separate
  duration-pill overlay that used to occupy that corner (duration remains
  shown as text in the info row below the thumbnail, where it was already
  duplicated) so the corner is unambiguously the face badge's alone. Every
  corner is now single-purpose: ★ star top-left, 🔔 notified bottom-left,
  👤 face-recognized bottom-right, selection checkbox top-right — any
  combination of star/notified/face can show simultaneously without ever
  competing for the same spot.

### Fixed — Codecov patch-coverage gaps from the 5.0.0 PR

- Closed all four lines Codecov's patch-coverage check flagged on the
  actual 5.0.0 pull request (`vision.py` 2 partial branches, `database.py`
  1 partial branch, `downloader.py` 1 partial branch) — confirmed the exact
  lines by running the same `pytest --cov-branch` CI uses locally and
  cross-referencing against the PR's real diff (`git merge-base` against
  `upstream/main`, which is pinned at 4.0.2), rather than guessing from
  file-level percentages alone. `downloader.py` had six other long-standing
  partial branches from well before this PR's base commit — left alone,
  since only the one from a July 13 commit (atomic local-storage downloads)
  was actually part of this patch. Added targeted tests for each: an
  object-detection model path with an explicit directory component (left
  untouched, not joined with the model cache dir), a subject/vehicle pair
  evaluated after a better one was already found (must not replace it), a
  depth comparison that comes back empty even though a pair was found, a
  local-storage download failing before any partial file was ever written
  (nothing to clean up), a face-bypass stats row with blank names (DB-layer
  defensive code, not reachable through analyzer.py's own invariants but
  not something that should crash the aggregation either), and a feedback-
  stats aggregate query defensively handling a `None` row (unreachable
  through real Postgres aggregate semantics without `GROUP BY`, which
  always return exactly one row — covered directly via a stand-in for the
  pool object, since asyncpg's real `Pool` doesn't allow monkeypatching
  individual methods).

### Fixed — clip Duration showed "—" for every real clip

- **Found while investigating why the duration overlay badge "wasn't
  showing" on the Library thumbnails** — checked the real live database
  directly rather than assuming a frontend display bug: all 143 real
  clips had `duration = 0`. Every one of them downloaded and plays
  correctly, so this was never a bad file — the main download path
  (`downloader.py`'s `_download_clip`) stores `duration` straight from
  the Blink API's own clip-list response, and on this account that field
  is consistently null/0 even for perfectly normal clips. (An earlier fix
  in this same changelog, "reconciled clips showed no Duration," assumed
  the normal download path was fine and only reconciliation needed a
  fallback — that assumption didn't hold up against real data.)
  Local-storage downloads had the same problem for a different reason:
  `duration` was simply hardcoded to `0`, since local-storage items never
  carry it in their own metadata at all. Fixed both the same way
  reconciliation already handled its own version of this problem: probe
  the actual downloaded file with `ffprobe` — duration is embedded in
  every video file's own container metadata regardless of what the API
  says. Moved the existing `library_scanner.py` probing helper (used only
  for reconciliation) into `downloader.py` as `probe_clip_duration`, used
  as a fallback whenever the API's own value is `<= 0` on the main path,
  and unconditionally for local-storage downloads; `library_scanner.py`
  now imports the shared version instead of keeping its own copy.
- **Existing clips already stored with `duration=0` don't get fixed by the
  above alone** — the fallback only runs at download time, and these
  clips were already downloaded. Added a startup backfill
  (`app.py`'s `_backfill_clip_durations`, mirroring the existing
  `_reimport_library` background task): scans for clips with
  `duration <= 0`, probes each still-present file, and updates the row.
  Self-limiting — a clip successfully backfilled has a real duration from
  then on, so later startups only re-check the shrinking remainder (or
  none, once caught up). New `database.py` methods
  `get_clips_missing_duration()` / `update_clip_duration()`.

### Improved — Library stats row decluttered

- **Removed the "Library size" stat** — redundant with the "💾 Storage"
  card right next to it, which already shows the same figure as "X MB
  used of Y GB quota."
- **Storage card is now a single text line + progress bar**, not three
  stacked text lines (a separate "STORAGE" title, a "used of quota" line,
  a "free on disk" line) plus the bar. Reported as making the stats row
  taller than it needed to be — `.lib-stats-row`'s `align-items: stretch`
  means every stat card matches the *tallest* card's height, so the
  storage card's extra lines were shrinking the clip grid's scroll area
  below it for every card in the row, not just itself.
- **Follow-up, same report**: even compacted, the storage card was still
  wide enough (a full sentence of text) that it always wrapped onto its
  own row below the other five stat cards, leaving visible empty space to
  their right and *still* pushing the clip grid down. Removed "Total"
  (redundant with the Status tab's own "Total clips" figure, same
  reasoning as the "Library size" removal above) and rebuilt the storage
  card to match the other cards' compact label/value layout ("💾 Storage"
  / "560.1 MB / 10 GB") instead of a sentence — it now fits on the same
  row as the others, top-right, matching their height instead of
  stretching them. Free-disk-space detail (previously its own visible
  line) moved to the card's hover tooltip — there's no room for a second
  line at this width, and it's secondary information next to the quota
  figure most users came to check.
- **Second follow-up**: centered the stats row (`justify-content: center`)
  so the empty space to the right of the last card is distributed evenly
  instead of all sitting on one side.

### Added — 👤 Recognized filter

- New checkbox filter next to ★ Starred/🔔 Notified, restricting the
  Library grid to clips with `face_recognized` true — same underlying
  `approved_faces_seen` condition the badge and stat already use. New
  `get_clips(recognized_only=...)` parameter (parameter-free, unlike
  `notified_only`/`min_confidence` — `approved_faces_seen` has no
  equivalent threshold to pass through) and `GET /api/clips?recognized=1`.

### Added — Biometrics: add more photos to an already-enrolled person

- Match accuracy scales directly with how many reference angles/distances/
  lighting conditions a person has enrolled — this already worked at the
  database level with zero changes needed (`face_enrollments` has no
  uniqueness constraint on `name`; every row is an independently-matched
  reference photo, and `enrollFace()` already just adds another one under
  whichever name it's given). The only real gap was discoverability: the
  Biometrics tab only ever offered a free-text Name field, with no
  guaranteed-exact-match way to target an existing person — a typo'd or
  differently-cased name would silently create a new, separate person
  instead of adding to the right one. Added a "— or —" dropdown of already-
  enrolled names next to the existing "+ Enroll" button, with its own
  "➕ Add to person" action that reuses the exact same clip-frame/photo
  picker and enrollment call, just guaranteed-targeting the selected
  person's exact name and preserving their current approval state rather
  than resetting it.

### Fixed — add-on failed to start on a real Home Assistant OS install

- **PostgreSQL init crashed the container's entire s6 boot** — surfaced only
  when testing an install on a real Home Assistant OS host (not the
  nested-Docker Supervisor devcontainer this add-on had been validated
  against up to this point, which turned out to be more permissive than real
  hardware here, the same way it previously was about AppArmor enforcement).
  `01-postgres-init.sh`'s `chown postgres:postgres /data/postgresql` failed
  with "Operation not permitted", `cont-init.d` treated that as fatal, and s6
  stopped the container before any service — including the media server —
  ever started. `apparmor.txt` granted broad `file,` access but no
  `capability` rules at all; AppArmor mediates Linux capabilities (`CAP_CHOWN`
  among them) separately from file access, and denies anything not
  explicitly listed even when Docker's own default capability set (which
  already includes `CAP_CHOWN`, unlike Supervisor's separate opt-in
  `privileged` config.yaml option, which covers a different, more dangerous
  set of capabilities and doesn't include it) would otherwise allow it. Added
  a bare `capability,` rule to `apparmor.txt` alongside the existing `file,`
  one.
- **The computer-vision pipeline could permanently break for the rest of a
  container's lifetime** after a burst of clips at startup (a backlog, or
  simply `concurrent_downloads` > 1) — every stage in `vision.py` guards its
  *own* first model load with an `asyncio.Lock`, but nothing stopped two
  *different* stages (e.g. object detection and face recognition) from each
  hitting their first-ever `import cv2`/`torch`/`ultralytics`/
  `facenet_pytorch` at the same moment on different threads, and
  `FrameEnhancer.enhance()` (unlike every other stage) imported `cv2`/
  `numpy` on every call with no protection at all. CPython's import
  machinery isn't safe against two threads both performing the first import
  of the same native extension module at once, and running Ultralytics
  object detection and biometrics face recognition concurrently made this a
  real, reproducible race rather than a theoretical one — once triggered, it
  left `numpy`/`cv2` raising `ImportError: cannot load module more than once
  per process` on every subsequent call, silently disabling object
  detection, enhanced-frame preprocessing, and face recognition (surfacing
  in the web UI as the Biometrics tab disappearing, since it hides itself
  when face recognition reports itself unavailable) for as long as the
  container kept running. Found via a real burst of concurrent clip analysis
  on a fresh Home Assistant OS install. A single lock now serializes every
  stage's first load against every other stage's.
- **Library's Recognized stat and filter stayed visible even when face
  recognition itself was unavailable** (see above) — unlike the Biometrics
  tab, which already correctly hides itself in that case. Checking the
  filter or reading the stat in that state was misleading (it can only ever
  read zero, since nothing was ever able to recognize anyone), and if
  availability flipped to unavailable while the filter was checked, clips
  stayed silently filtered by a criterion no longer shown anywhere. Both now
  hide behind the same availability check, and the filter clears itself if
  availability drops while it's checked.
- **The CV pipeline/face-recognition CPU requirement was undocumented in the
  web UI itself** — a Raspberry Pi 4 (or older) owner would just see the
  Biometrics tab and Library's Recognized filter silently missing, with no
  explanation anywhere they'd actually be looking. README.md and DOCS.md
  already covered it, but the in-app Models tab (the one static/reference
  page shown to everyone regardless of hardware) didn't. Added a hardware
  note there matching the existing one for `moondream_local`'s GPU
  requirement — confirmed the underlying detection itself already correctly
  reports every x86_64 host (any age) and Raspberry Pi 5+ (any ARMv8.1+
  Cortex-A76-class CPU) as compatible.
- **There was no way to get a Home Assistant notification specifically for
  suspicious clips.** `notify_ha` fires a persistent notification on *every*
  new-clip download, plus the daily digest and system events (2FA/auth
  failures, storage full) — useful for some, but far too noisy for anyone
  who only wants to know when something's actually flagged. Mobile
  push/email/Discord already had this suspicious-only behavior via
  `NotificationDispatcher.dispatch()`; HA persistent notifications didn't.
  Added a new, independent `notify_ha_suspicious` option (default `false`)
  wired into that same dispatch path — alongside a **Send test HA
  notification** button on the Automations tab's Notification Channels
  panel, matching the existing email/Discord/mobile test buttons. Also
  fixed DOCS.md's `notify_ha` default, documented as `true` but actually
  `false` since it was introduced.

## 4.0.2

### Bug fixes

- **Fix: the AI Usage tab's Per-Model Breakdown could show the same
  escalation model twice.** `get_token_usage_stats` grouped escalation rows
  by `(escalation_model, escalation_provider)`; rows written before the
  `escalation_provider` column existed backfill to `''` via the schema
  migration, so once newer rows carried the real provider value, the same
  model split into two duplicate-looking rows. Escalation rows are now
  grouped by `escalation_model` alone (matching how tier-1 rows were always
  grouped), with a representative non-empty provider surfaced for the
  label.

### New features

- **Added a "Daily Usage (Last 14 Days)" table to the AI Usage tab**,
  showing per-day analyses, total tokens, and estimated cost so usage
  trends are visible without leaving the tab. Each day's tokens are priced
  per-model server-side (same approach as the Per-Model Breakdown) before
  being summed into a single daily total; days with no analysis activity
  are simply omitted rather than zero-filled, keeping the table small.

## 4.0.1

Bug-fix release addressing two real-world accuracy reports: a second
protected-vehicle miss (distinct from the one fixed in 4.0.0) traced to the
two-tier escalation system itself, and a pattern of ordinary front-door
coming-and-going being flagged suspicious.

### Bug fixes — two-tier escalation

- **Fix: the two-tier escalation system only ever double-checked a
  *suspicious* tier-1 verdict, never a "clear" one.** A user leaning
  against, and resting a foot on, their protected vehicle was analyzed as
  "clear" at 89% confidence by tier 1 — because that verdict wasn't
  suspicious, tier 2 was never consulted, so a stronger model never got the
  chance to catch what tier 1 missed. `_maybe_escalate` now applies a
  high-recall policy on protected-vehicle cameras (`ai_car_cameras`, or
  every camera when that list is empty): tier 2 is always consulted, and a
  suspicious verdict from *either* tier wins. Every other camera keeps the
  original, cost-optimized behavior — a non-suspicious tier-1 result is
  still trusted outright and never escalated, since those cameras should
  flag less, not more.

### Bug fixes — front-door false positives

- **Fix: brief, ordinary door interactions (unlocking, opening, stepping
  out to leave) were frequently flagged suspicious** despite the prompt's
  existing explicit rule that this is routine. Added a code-computed SHORT
  EVENT hint: a clip at or under `_SHORT_EVENT_DURATION_SECONDS` (10s) now
  tells the model that a brief, single interaction is far more consistent
  with routine coming-and-going than with lingering, casing, or tampering —
  framed as a hint the model can still override for a clip that visibly
  shows real tampering.
- **Fix: a thumbs-down with no typed note carried no reusable signal.**
  `get_prompt_corrections` only folds corrections with a non-empty note
  into future prompts, so a bare "incorrect" click — the most common way to
  rate a clip — taught the system nothing. The feedback API now
  auto-generates a correction note from the direction of the correction
  (over-flagged vs. missed) whenever the reviewer doesn't type one, so
  every negative rating becomes usable few-shot guidance.

### Internal

- De-duplicated three copies of the "does protected-vehicle protection
  apply to this camera" check into a single `_car_protection_applies`
  helper, shared by prompt-building and the new escalation policy.

## 4.0.0

Major release: a security-intelligence pass on the AI analysis pipeline,
prompted by a real-world false negative (a user leaning on, touching, and
resting a foot on their own protected vehicle went unflagged, with the
description incorrectly stating "not the protected vehicle").

### Bug fixes — protected-vehicle accuracy

- **Fix: a person directly touching the protected vehicle could go
  unflagged when Moondream's vehicle disambiguation guessed the wrong
  box.** `_detect_protected_vehicle`/`_detect_protected_vehicle_sync` run
  two independent zero-shot `/detect` calls on the same frame — a generic
  "car" query and a description-specific query — to tell the protected
  vehicle apart from any other visible vehicle. These two calls can
  legitimately draw slightly different boxes for the *same* physical car
  (more so the moment a person leans on/touches it, since their body
  changes what's visibly "car"), which was enough to push the boxes'
  IoU below the match threshold and misclassify the real vehicle as
  "another vehicle" right at the moment contact happens. Proximity is now
  measured against every detected car box, not just the one disambiguation
  labelled "protected" — vehicle identity still gates the vehicle-to-vehicle
  case (a second car parked nearby), but a person's contact with *any*
  vehicle on a car-protected camera is never missed on an identity guess.
- **Fix: an unusual `ai_car_description` (e.g. including a license plate
  number) could derail Moondream's disambiguation entirely.** A plate
  number isn't a visual feature a zero-shot detector can ground, so
  including it in the detect query risked matching the wrong region or
  nothing at all. The description shown to the model in the text prompt is
  unchanged (a plate there is useful reasoning context), but the text sent
  to the `/detect` API call is now stripped of plate mentions first.
- **Fix: the shared prompt (all 6 providers) had no guidance for the
  "only one vehicle visible" case**, so an unconfirmed color/plate match
  under night/infrared (often grayscale) conditions could make the model
  hedge and describe a person's contact with their own car as involving
  "the dark car, not the protected vehicle." The prompt now explicitly
  says: if only one vehicle is visible at all, treat it as the protected
  one by default — never withhold a contact finding over an unconfirmed
  color/plate/make detail.
- **Fix: routine lawn equipment/wind-blown debris rules didn't distinguish
  "merely nearby" from "actually causing damage."** A rock flung by a
  mower, or a trash bin blown with force into the vehicle, was previously
  suspicious=false alongside ordinary yard maintenance. The rule now
  splits: no-contact proximity stays routine, but a visible strike, dent,
  or scrape is suspicious=true regardless of whether a person was at
  fault. Animal contact (a dog jumping on or urinating on the vehicle) is
  also now explicit rather than folded into "otherwise investigate."
- **Fix: the add-on manifest's default `ai_prompt` (`config.yaml`, what a
  fresh install actually gets) had drifted out of sync with the richer
  Python-level default in `config.py`**, missing explicit coverage for
  mail/package theft, security-camera tampering, casing behavior, and the
  delivery-vs-theft distinction. Both defaults are now identical (enforced
  by a new test), so a new install's actual prompt matches the one this
  project has been tuning all along.

### New feature — car zones

- Added an optional **car zone**: a per-camera rectangle (set via the
  Camera Configurations panel in the web UI's AI tab, shown once "Protected
  vehicle visible from this camera" is checked) marking roughly where the
  protected vehicle normally sits. Since Blink cameras are fixed in place,
  this is stable ground truth that doesn't depend on any single frame's
  object detection succeeding.
  - A **zone-motion** signal is computed per clip (reusing the existing
    grayscale frame-diff "smart brain" machinery, no new dependencies) and
    fed into the prompt as structured evidence: what share of the clip's
    overall motion actually happened inside the configured zone versus
    elsewhere in the frame.
  - For Moondream providers, the zone is also used as a **fallback
    proximity reference** — if a clip's car detect finds no vehicle at all
    this frame, a person standing where the car normally sits still
    produces a proximity hint instead of silently applying none.
  - Considered adding real OpenCV (`cv2`) for this — ruled out for this
    add-on specifically because this Docker image is Alpine-based and
    `opencv-python-headless` publishes no musllinux wheels, so it would
    require compiling OpenCV from source in the build (slow, fragile, the
    same class of problem already documented for `moondream_local` on
    aarch64). The zone-motion feature reuses the existing Pillow-based
    frame-diff approach instead, with identical functional value and zero
    Docker build risk.

### Testing

- New/updated tests for all of the above; `media_server.py` reaches 100%
  coverage, overall project coverage holds at 99.4%.

## 3.2.0

Bug-hunting pass across the codebase (excluding `analyzer.py`, covered
previously), prompted by a user report of routine front-door coming-and-going
being flagged suspicious.

### Bug fixes

- **Fix: `/api/activity?days=N` silently returned no data for `days<=0`.**
  `_handle_activity` only clamped the upper bound (`min(..., 30)`) — a zero
  or negative value shifted the activity query's cutoff to today or into the
  future, so the endpoint returned an empty result instead of erroring or
  behaving sensibly. Now clamped to `max(1, min(..., 30))`, matching the
  `limit`/`offset` clamping already used by `_handle_list_clips` and
  `_handle_ai_suspicious` in the same file.
- **Fix: `/api/ai/analyze/{clip_id}` ("Analyze Now" button) could return a
  raw HTML 500 error page instead of a clean JSON error.** Unlike the
  sibling `/api/ai/test` endpoint, this handler didn't wrap the
  `analyze_clip()` call in a try/except — an unexpected analyzer failure
  (network blip, corrupt clip, etc.) would propagate out of the handler and
  surface as aiohttp's generic error page, breaking the web UI's error
  display instead of showing `{"error": "..."}`. Now mirrors `/api/ai/test`'s
  error handling.

### AI prompt

- **Fix: a resident leaving through the front door could be flagged
  suspicious.** The default `ai_prompt`'s NOT SUSPICIOUS rules only
  explicitly covered a person walking up to the front door, opening it, and
  going *inside* — opening the door and stepping *out* to leave (for work,
  to take out trash, walking to a car, etc.) had no matching carve-out, so
  it fell through to a more alarming reading purely for "opening a door and
  walking away." Added a symmetric rule: exiting the front door and calmly
  walking off is exactly as routine as entering, unless the person then
  lingers, repeatedly looks around, or otherwise matches one of the existing
  SUSPICIOUS behaviors (casing, fleeing after tampering, etc.) — those are
  untouched, so this only narrows the specific "calm exit" false-positive
  pattern and does not weaken detection of genuine intrusion/tampering
  behavior.
- Note: true person-recognition ("learn who I am") would need a much larger
  feature (face embeddings, a trusted-persons registry, biometric data
  handling) with real privacy tradeoffs — not attempted here. See the
  conversation notes for the fuller discussion of why the prompt fix above
  was chosen instead.

### Testing

- Added regression tests for both media-server fixes and the new AI-prompt
  carve-out. `media_server.py` reaches 100% coverage; overall project
  coverage holds at 99.4%.

## 3.1.9

Static-analysis cleanup pass (SonarQube). No user-facing behavior changes.

### Bug fixes

- **Fix: the moondream local-install background task could be garbage
  collected mid-install.** `asyncio.create_task()`'s result wasn't kept
  anywhere — asyncio only holds a weak reference to a running task, so an
  unreferenced task can be silently collected before it finishes. The task
  is now held on the `MediaServer` instance for its duration.
- **Fix: two `except` clauses listed `json.JSONDecodeError` alongside
  `ValueError`.** `JSONDecodeError` is a `ValueError` subclass, so the extra
  entry was redundant (harmless, but flagged as dead code) in the cached
  Blink auth loader (`downloader.py`) and the daily-digest state loader
  (`digest.py`).

### Code quality

- Replaced several `logger.error(...)` calls inside `except` blocks with
  `logger.exception(...)` so the full traceback is captured in the add-on
  log for genuinely unexpected failures (Moondream model loading/fine-tune
  calls, Blink authentication, Anthropic/OpenAI API errors) — same log
  level, more diagnostic detail, no behavior change.
- Two floating-point equality checks (`confidence == 0.0`, `gap == 0.0`)
  were rewritten as `<= 0.0`. Both values are already clamped to be
  non-negative before the comparison, so this is behaviorally identical but
  no longer trips a "don't compare floats for equality" warning.
- Extracted two nested ternary expressions (subject left/right and top/
  bottom framing in the AI position hint) into plain `if`/`elif`/`else`
  statements for readability.
- De-duplicated the repeated vision-provider system prompt (Ollama,
  Anthropic, OpenAI each had their own copy of the same 5-line string) into
  one shared `_VISION_SYSTEM_PROMPT` constant, and the repeated `"Clip not
  found"` HTTP 404 literal in `media_server.py` into `_CLIP_NOT_FOUND`.
- Reduced cognitive complexity in two functions by extracting a helper with
  identical behavior: `StorageManager._apply_retention_policy` (the per-file
  delete-if-expired check moved to `_delete_if_expired`) and
  `HAEventWatcher._connect_and_watch` (the per-message WebSocket handling
  moved to `_handle_ws_message`).
- Converted `BlinkClipDownloaderApp._write_stats` from `async def` to a
  plain method — it did no `await`-ing of its own and both call sites were
  plain sequential calls, not part of any interface that requires it to stay
  a coroutine.
- Fixed a stray U+2013 "–" (en dash) in `__init__.py`'s module docstring,
  replaced with a plain ASCII hyphen.

### Reviewed, left unchanged (documented reasoning)

A larger set of SonarQube findings were reviewed and intentionally left as
they are — either because the "fix" would change or risk behavior for a
purely cosmetic gain, or because the finding doesn't hold up once this
add-on's specific design is taken into account:

- **"Use asynchronous features or remove `async`" (~20 locations).** Verified
  every instance: they're either (a) `@abc.abstractmethod` declarations on
  `BaseAnalyzer` (`health_check`, `fetch_models`, `_call_model`, `close`)
  that every concrete provider subclass overrides with real `await`-ing
  code, (b) a `_get_session()`/`stop()`/`close()` lifecycle method called
  generically through a uniform async interface elsewhere (e.g.
  `app.py`'s `_shutdown_step`), or (c) an aiohttp route handler, which
  **must** be `async def` to satisfy `aiohttp`'s router regardless of
  whether that specific handler awaits anything. Removing `async` from any
  of these would require rewriting the calling convention everywhere that
  provider/handler is used polymorphically, for no functional benefit.
- **"Ensure `asyncio.CancelledError` is re-raised" (`analysis_queue.py`,
  `event_watcher.py`).** Both loops deliberately catch cancellation to exit
  their `while` loop cleanly on shutdown instead of propagating — this is
  intentional, existing, and covered by a test for each
  (`test_is_in_schedule_uses_local_time_not_utc`-style regression tests
  exist for this exact "breaks the loop without re-raising" behavior).
  Re-raising would skip that clean-exit path for no observable benefit,
  since both tasks are already cancelled and gathered with
  `return_exceptions=True` during shutdown.
- **Cognitive Complexity (~15 functions, up to 65).** Left as-is beyond the
  two smallest/safest extractions above. These are large, business-critical
  functions (AI prompt building, config loading, database queries, app
  orchestration) where a mechanical split carries real risk of changing
  control flow in a subtle way, for a style-only benefit. Happy to tackle
  specific ones on request.
- **`create_analyzer` has 20 parameters (limit 13).** Bundling them into a
  config object would touch the one call site and every test that
  constructs an analyzer — deferred as a larger, lower-value change.
- **Dockerfile: "image might run as root."** Confirmed intentional and
  required: this add-on's base image starts via s6-overlay's `/init`
  (documented in the Dockerfile), which needs root to supervise services
  inside the container — this is the standard pattern for virtually all
  Home Assistant add-ons. Security is instead enforced via the add-on's
  `apparmor.txt` profile (confines filesystem/capability access) plus
  Supervisor-managed container isolation, not a non-root Docker `USER`.
  Switching to a non-root user would very likely break add-on startup.

### Testing

- Added ~20 regression tests closing previously-uncovered branches found
  while verifying the fixes above, focused entirely on `app.py`'s main
  orchestration (`run()`/`_shutdown()`'s conditional background-task and
  cleanup paths for the media server, HA event watcher, and AI analysis
  queue; `_connect_with_retry`'s interruptible per-second wait;
  `_wait_with_trigger_check`'s fast-poll and trigger-file-unlink-failure
  branches; anomaly-baseline recording in `_on_clips_downloaded`) — these
  were only reachable with config flags the shared test fixture disables by
  default. `app.py` went from 89% to 100% coverage; overall project coverage
  raised from 98.5% to 99.4%.

## 3.1.8

### New features

- **Add-on icon and logo.** The add-on previously used the Supervisor's
  generic puzzle-piece placeholder; it now has a dedicated `icon.png` and
  `logo.png`.
- **Custom confirmation modal for "Clear Stats".** Replaced the native
  browser `confirm()` dialog (which HA ingress iframes can suppress or
  render inconsistently) with an in-app modal, matching the styling of the
  rest of the web UI. Only this button was changed — clip-delete
  confirmations are unaffected.
- **"Send Test Email" button in the AI tab.** Once `smtp_host` and
  `smtp_recipients` are configured, an Email Alerts card appears with a
  button that sends a one-off test email — even while `smtp_enabled` is
  still `false` — so SMTP credentials can be verified before waiting for a
  real suspicious-activity alert.
- **Car-protection status surfaced in the web UI.** The AI tab now shows a
  warning banner under Camera Configurations when a camera is checked
  "Protected vehicle visible from this camera" but `ai_car_description` is
  empty — previously the checkbox silently had no effect in that case with
  no indication why.
- **Shorter add-on store description that leads with AI analysis.** The
  Supervisor add-on card previously truncated the description mid-sentence;
  it's now short enough to display in full and mentions AI-powered
  suspicious activity detection up front.

### Bug fixes

- **Fix: a truncated or malformed escalation-tier (tier 2) response could
  silently suppress a genuine suspicious alert.** When `openai_escalation_model`
  is configured, a suspicious tier-1 verdict is re-checked by the stronger
  model; if that tier-2 response came back non-empty but truncated (e.g. a
  reasoning model's invisible "thinking" tokens consumed its completion
  budget before the JSON closed), it failed to parse and was treated as an
  implicit `suspicious: false` — silently overriding and discarding tier 1's
  correct suspicious call, with no alert sent. The escalation call now
  verifies tier 2's response is syntactically complete JSON before trusting
  it; a truncated/malformed tier-2 response now falls back to tier 1's
  verdict instead of being mistaken for a real "not suspicious" result.
- **Fix: `ai_schedule_start`/`ai_schedule_end` were compared against UTC
  instead of local time.** The analysis schedule window is documented (and
  configured) in local wall-clock time, matching `digest_time` elsewhere in
  the add-on, but the queue was checking it against `datetime.now(UTC)` —
  on any host not already in UTC, the configured window silently ran on the
  wrong hours.
- **Fix: `time_window_start`/`time_window_end` clip filtering compared
  against UTC instead of local time.** Same class of bug as above, in the
  downloader's per-clip time-window filter: Blink's `created_at` timestamp
  is UTC and was compared directly against the documented-as-local window
  bounds without converting to local time first.
- **Fix: a camera name of `.` or `..` could resolve outside the configured
  storage directory.** `_safe_name()` sanitizes camera/clip names for use as
  filesystem path components but left an all-dots result (e.g. `".."`)
  untouched, which `StorageManager.resolve_path()` would then use as a
  literal path segment — a malformed or malicious camera name could escape
  the configured base download directory. All-dots results now fall back to
  `"unknown"` like other unusable names.
- **Fix: a disk I/O error mid-download (e.g. disk full) left a permanent
  partial clip file on disk.** `_stream_to_file()` retried on network errors
  (`aiohttp.ClientError`/`asyncio.TimeoutError`) but not `OSError` raised
  while writing chunks to disk — an `OSError` skipped the retry/cleanup path
  entirely, leaving a truncated file behind that a later poll's
  `dest.exists()` pre-check would mistake for a completed download,
  permanently. `OSError` is now caught alongside the network errors.
- **Fix: several genuinely suspicious car-camera and general scenarios were
  not covered by the default AI prompt.** Added explicit rules for: another
  vehicle making physical contact with the protected vehicle (bump/scrape/
  sideswipe, including hit-and-run); a person tampering with a security
  camera itself; repeated door/lock handle manipulation beyond normal use;
  casing behavior (repeatedly peering into windows/vehicles or pacing with
  no clear purpose); and the mail/package-theft signature of picking up a
  delivery and leaving instead of entering the home. Also added explicit
  non-suspicious carve-outs for routine lawn equipment operated near a
  vehicle and wind-blown debris contacting a vehicle or property, so those
  don't get flagged just because they bring an object physically close to
  the protected vehicle.
- **Fix: default AI prompt could under-flag genuinely suspicious activity
  on car/driveway cameras while over-flagging routine front-door entries.**
  The default `ai_prompt` required ALL of three unrelated behaviours to
  co-occur for a "suspicious" verdict — a bar routine activity already
  clears by matching just one of them (e.g. "touching the door"), while
  effectively suppressing rarer but genuinely suspicious car-camera events.
  Rewritten as a clearer set of specific ANY-of-these-applies criteria
  (tampering with a lock, entry through an unusual opening, hiding beside
  property, fleeing after contact) with explicit non-suspicious examples
  for ordinary door use. The prompt's definition of "confidence" was also
  corrected from image clarity (which systematically favours well-lit
  cameras like a front door over dimmer or farther ones) to certainty in
  the suspicious/not-suspicious verdict itself.
- **Fix: `openai_escalation_model` had no label or description in the
  Configuration tab.** It rendered as the raw snake_case key with no
  explanation; added a proper translation entry.
- **Fix: missing `claude-sonnet-5` pricing entry.** Anthropic's Claude
  Sonnet 5 was not in the pricing table or fallback model list, so
  selecting it fell back to a generic/incorrect rate; added with its
  current tiered rate.

### Documentation

- Added a prerequisite note under Per-Camera Configuration: the "Protected
  vehicle visible from this camera" checkbox does nothing until
  `ai_car_description` is also set.
- Documented the new "Send Test Email" button under Email (SMTP).

### Testing

- Added regression tests for every fix above, plus additional coverage for
  previously-untested error paths in the OpenAI two-tier escalation flow,
  Blink connect()/2FA edge cases, clip-list pagination and time-window
  filtering, and USB Sync Module local-storage downloads. Overall coverage
  raised from 97.3% to 98.5%.

## 3.1.7

### New features

- **Escalation count and cost now tracked in the AI Usage tab.** When
  `openai_escalation_model` is configured, escalation-tier analyses now
  appear as their own row in the AI Usage panel (tagged "escalated"),
  with their own token counts and cost estimate, plus a new "Escalations"
  total. Previously escalation-tier tokens were silently merged into the
  tier-1 model's totals and never shown separately.
- **"Clear Stats" button for the AI Usage tab.** Lets you reset the
  token/cost/escalation counters — useful after switching AI providers so
  old provider's usage doesn't keep accumulating into the total. This only
  resets the displayed counters; per-clip analysis history (Suspicious
  Clips, clip detail view) is untouched.
- **Escalation token count now shown in the AI Usage tab.** A new
  "Escalation Tokens" stat sits alongside "Escalations" so you can see the
  token cost of tier-2 re-analysis at a glance, not just how many clips
  were escalated.
- **Copy button next to "Fetch Models".** After fetching the live model
  list, click **Copy** to copy the selected model id to the clipboard for
  pasting into `openai_model`/`anthropic_model`/`ollama_model` in the
  add-on configuration. There's no supported way for an add-on to write
  directly into another add-on's YAML options from its own web UI, so copy
  and paste remains the safest option.
- **OpenAI: adopted Structured Outputs for schema-conformant JSON.**
  `gpt-4o`, `gpt-4.1`, the `gpt-5` family (including `gpt-5.4-nano` and
  `gpt-5.4`), and `o4-mini` now request `response_format:
  {"type": "json_schema", ...}` with `strict: true` instead of the looser
  `json_object` mode, guaranteeing the `suspicious`/`confidence`/
  `description` fields are always present with the right types. Older
  models that don't support Structured Outputs (e.g. `gpt-4-turbo`) keep
  using `json_object`.
- **OpenAI: reasoning models now use `reasoning_effort: "low"` and a larger
  token budget.** `o1`/`o3`/`o4`/`gpt-5`-family requests now set
  `reasoning_effort="low"` — appropriate for this add-on's single-verdict
  classification task — and `max_completion_tokens` was raised from 512 to
  1024, since these models bill invisible reasoning tokens from the same
  budget as the visible completion and 512 could leave too little room for
  the actual JSON response on a harder clip.

### Bug fixes

- **Fix: AI Usage cost estimate used a single blanket rate for all rows.**
  Cost is now computed per model/tier using that model's actual pricing,
  so usage from multiple providers or models no longer gets mis-priced
  against whichever model happened to be selected most recently.
- **Fix: `fetch_models()` mis-priced dated OpenAI/Anthropic model
  snapshots.** The live models list can return dated ids like
  `gpt-4o-mini-2024-07-18` instead of the bare alias; pricing lookup now
  matches by prefix instead of requiring an exact key match, so these
  snapshots get correct pricing instead of falling back to a generic
  default.
- **Fix: stale/incorrect OpenAI pricing for `o3` and `o1-mini`.** Both were
  priced against outdated per-token rates; corrected to match OpenAI's
  current published pricing. `gpt-5.2` also had no explicit entry and was
  silently mispriced by falling through to the bare `gpt-5` rate; it now
  has its own entry (`gpt-5.1` was added alongside it for clarity, though
  it already resolved to the same rate via fallback).
- **Fix: `reasoning_effort: "low"` could be sent to "-pro" tier reasoning
  models** (e.g. `gpt-5.2-pro`), which the OpenAI API only accepts `"high"`
  for and would reject the request. These models are no longer sent a
  `reasoning_effort` value, letting the API fall back to its own default.

## 3.1.6

### New features

- **GPT-5 family support for the OpenAI provider.** `gpt-5`, `gpt-5-mini`,
  `gpt-5-nano`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, and `gpt-5.5` are
  now recognized as vision-capable models with accurate pricing shown in
  **Fetch Models** and the AI usage panel. `gpt-5.5-pro` is intentionally
  excluded — it's only available via OpenAI's Responses/Batch APIs, not the
  Chat Completions API this add-on uses.
- **Two-tier escalation for the OpenAI provider.** A new `openai_escalation_model`
  option lets you pair a cheap/fast model (`openai_model`) with a stronger one:
  every clip is analyzed with the cheap model first, and only clips it flags as
  suspicious get a second, more careful analysis from the escalation model,
  whose verdict is what's recorded and alerted on. Most motion clips aren't
  suspicious, so this cuts cost on the common case while still applying a
  stronger model where it matters. Leave `openai_escalation_model` empty
  (the default) to keep the previous single-model behaviour.
- **OpenAI cost estimate now shown in the web UI.** The AI usage panel's
  "Estimated cost" figure previously only appeared for the Anthropic
  provider even though OpenAI pricing/token tracking was already
  implemented — it now also renders for `openai`.

### Bug fixes

- **Fix: OpenAI requests to `gpt-5`-family and `o1`/`o3`/`o4-mini` models
  failed with HTTP 400 `Unsupported parameter: 'max_tokens'`.** These models
  reject the legacy `max_tokens` chat-completions parameter and require
  `max_completion_tokens` instead; the OpenAI analyzer now selects the
  correct parameter based on the model name.
- **Fix: a suspicious tier-1 verdict with an empty `description` field never
  escalated to the tier-2 model.** Two-tier escalation required both
  `suspicious=true` and a non-empty description before re-analyzing with
  `openai_escalation_model`; a terse tier-1 response that set `suspicious`
  but left `description` blank silently skipped the stronger second look it
  was supposed to get. Escalation now triggers on `suspicious=true` alone.
- **Fix: repeated, uncached health-check calls to cloud AI provider APIs
  showed up as constant traffic in the logs.** The web UI polls the AI
  status endpoint every 10 seconds while the AI tab is open, and the
  background analysis queue polls independently every `ai_check_interval`
  seconds — each poll triggered a fresh authenticated API call (e.g.
  OpenAI's `GET /v1/models`) for every cloud provider (OpenAI, Anthropic,
  Ollama Cloud, Moondream Cloud). Health-check results are now cached for
  30 seconds per provider, cutting redundant API traffic without affecting
  how quickly a real outage is detected.
- **Fix: the OpenAI provider was missing from the AI status card's model
  picker.** The "Fetch Models" button never appeared for `openai` in the AI
  status card due to a missing provider entry, even though the picker
  worked correctly in the AI Usage tab.

### Documentation

- Clarified in `config.yaml`, `DOCS.md`, and the web UI's AI usage notes
  that two-tier escalation (`openai_escalation_model`) is an OpenAI-only
  feature — it has no effect for any other provider, and is not available
  for Moondream Cloud in particular since that provider only exposes a
  single selectable model.

## 3.1.5

### AI analysis improvements

- **Fix: parked/passing vehicles near the protected vehicle were flagged as
  "suspicious" with no person involved.** Users were seeing notifications
  like "A silver Kia Forte is parked in the driveway, close to the
  protected vehicle" or "Three cars are parked in the driveway" — routine
  parking with no person or animal in frame, marked suspicious purely on
  vehicle-to-vehicle proximity. The `PROTECTED VEHICLE` prompt rules (used
  by every provider) now state that another vehicle parking, stopping, or
  passing near the protected one is always `suspicious=false` when no
  person or animal is involved, no matter how close it parks — only a
  person or animal actually touching, lingering near, or reaching toward a
  vehicle is suspicious.
- **Moondream (Cloud and local): the above policy is now also enforced in
  code, not just the prompt.** Moondream's small vision-language model
  doesn't reliably honor negative instructions, so a frame where detection
  found only vehicle-to-vehicle proximity (no person or animal) now has its
  `suspicious` verdict forced to `false` after the model responds,
  regardless of what the model itself reported — closing the gap that let
  a "close to the protected vehicle" description slip through as a
  notification anyway. `_vehicle_proximity_hint()` was also reworded to be
  unconditional rather than gap-dependent, since even a vehicle genuinely
  stopping or parking close by isn't a security concern by itself.

## 3.1.4

### New features

- **Notification filter for the clip library.** A "🔔 Notified" checkbox in
  the Library filter bar (alongside "★ Starred") restricts the clip grid to
  clips whose AI analysis was suspicious at or above the currently
  configured `ai_min_confidence` — the same clips that would have triggered
  a notification — so you no longer have to scroll through every clip to
  find the ones worth reviewing. Clips matching this now also show a 🔔
  badge on their thumbnail. Backed by a new indexed, schema-migration-free
  `EXISTS` query in `get_clips()`, so it scales the same way existing
  filters do even with thousands of clips.

### AI analysis improvements

- **Refined protected-vehicle scoping for multi-car scenes.** The
  `PROTECTED VEHICLE` prompt section now explicitly tells the model that
  when more than one vehicle is visible (e.g. an apartment parking lot, a
  neighbor's car, street parking), the distance/tampering rules apply only
  to the vehicle matching the configured description — a different vehicle
  parked or passing nearby is not itself suspicious. This directly
  addresses multi-vehicle properties where the described car needs to be
  identified and monitored as "the" protected vehicle, distinct from any
  other car in frame.

### Bug fixes

- **Fix: a non-numeric `confidence` value in an otherwise-valid AI JSON
  response crashed that clip's analysis.** `_try_parse_json()` parsed
  `confidence` with `float(...)` outside the `try`/`except` that guards
  JSON decoding, so a provider returning valid JSON with e.g.
  `"confidence": null` raised an uncaught `TypeError` instead of falling
  back to `0.0` like every other malformed-response case.
- **Fix: `download_local_storage_clips()` never persisted the download
  tracker.** Unlike `download_new_clips()`, the Sync Module USB
  local-storage download path only called `tracker.mark_downloaded()` in
  memory and relied on the clean-shutdown `save()` to flush it — an
  add-on container OOM-kill or ungraceful Supervisor stop between polls
  would lose those records, causing already-downloaded local-storage clips
  to be re-counted (though not re-downloaded or corrupted) on the next
  restart. Both the normal completion path and the early return on a
  storage-quota breach now call `tracker.save()` directly, matching
  `download_new_clips()`.

## 3.1.3

### AI analysis improvements

- **Reduced low-value/false-positive suspicious-activity notifications.**
  Several changes work together to cut down on alerts describing static
  background scenery, routine passersby, or low-confidence hedges instead of
  genuine security concerns:
  - The analysis prompt now explicitly states that a scene-baseline
    deviation is frequently just lighting, weather, shadows, or a day/night
    transition, and instructs the model to set `suspicious=false` unless a
    specific new person, animal, or vehicle is clearly visible.
  - A new prompt rule ties `suspicious=true` to a documented confidence
    floor of 0.5 — the model is told this is not a hedge value, and that an
    ambient scene change alone is never sufficient grounds for a suspicious
    verdict.
  - The "a person or animal simply passing through is not suspicious"
    guidance is now camera-agnostic instead of only applying to cameras with
    a protected vehicle configured, so false-positive reduction benefits
    every camera, not just driveway/car cameras.
  - `ai_min_confidence` now defaults to `0.5` instead of `0.0`, aligning the
    alert-dispatch gate with the confidence floor the prompt itself uses for
    a genuine suspicious verdict, so low-confidence guesses no longer
    trigger notifications (results are still analyzed and stored either
    way).

### Bug fixes

- **Fix: Moondream's vehicle-disambiguation pipeline could report a car
  parked alone as "another vehicle stopped right next to the protected
  vehicle."** A single parked car sometimes produces two overlapping
  bounding boxes from Moondream's generic `car` detection query (e.g. a
  full-body box and a tighter crop of the same car). These duplicate boxes
  were never deduplicated against each other before being compared to the
  protected-vehicle detection result, so the second box for the *same* car
  was mistaken for a second, distinct vehicle. Both `MoondreamCloudAnalyzer`
  and `MoondreamLocalAnalyzer` now collapse heavily-overlapping duplicate
  boxes (IoU ≥ 0.3) before running vehicle disambiguation.

## 3.1.2

### Bug fixes

- **Fix: the Home Assistant Supervisor token could be sent to a
  user-configured Discord webhook or mobile-notification service.**
  `HANotifier` and `NotificationDispatcher` each attached the Supervisor
  bearer token as a default header on their shared `aiohttp.ClientSession`,
  so *every* request made through that session carried it — including
  `call_webhook()` and `send_discord()`, which POST to an arbitrary
  user-supplied URL. The token is now attached per-request only on the
  actual HA API calls (`_post()`'s notify call, `send_mobile()`), so
  third-party webhook/Discord requests never see it.
- **Fix: SMTP notifications over port 465 hung or failed the TLS
  handshake.** Email always sent with `start_tls=True`, but port 465 is
  *implicit* TLS (the socket is TLS from the first byte) — an
  incompatible negotiation from STARTTLS on 587/25. The SMTP channel now
  branches on the configured port and passes `use_tls=True` instead for
  465.
- **Fix: reflected XSS via the `X-Ingress-Path` request header.** The web
  UI's `_handle_index()` interpolated this header directly into a
  `<script>` block (`_HTML.replace("'__HAROOT__'", f"'{ingress_path}'")`)
  with no escaping, so a value like `'};alert(1);//` broke out of the JS
  string literal. The header is now serialized with `json.dumps()` (proper
  quote/backslash escaping) with `</` additionally swapped to `<\/` so a
  value containing `</script>` can't close the surrounding tag early.
- **Fix: stored XSS via camera names, tags, and clip source fields
  rendered in the web UI.** Several `innerHTML` call sites (camera list,
  clip cards, the clip detail modal, tag chips, the status page) inserted
  these values without escaping. All of them now go through `_esc()`.
  `_esc()` itself was also fixed: its textContent round-trip escaped `&`,
  `<`, `>` but left quote characters raw, which was exploitable anywhere
  the result was placed inside a quoted HTML attribute (e.g.
  `data-tag="${_esc(t)}"`) — it now also escapes `"` and `'`.
- **Fix: negative `limit`/`offset` query parameters could bypass
  pagination and dump the entire clip or suspicious-clips table.** SQLite
  treats a negative `LIMIT` as "no limit" and a negative `OFFSET` as
  invalid; `_handle_list_clips()` and `_handle_ai_suspicious()` now clamp
  both to non-negative values.
- **Fix: concurrent `analyze_clip()` calls on the same analyzer instance
  could corrupt each other's results.** `analyze_clip()` stashes per-call
  state (`_current_camera`, token counters) on `self` across many awaited
  I/O calls, which is only safe if calls never interleave — an assumption
  the background `AnalysisQueue` and the media server's on-demand
  "Analyze Now"/"Test" HTTP handlers (which share one analyzer instance)
  could violate. `analyze_clip()` now serializes its body behind a
  per-instance `asyncio.Lock`.
- **Fix: a timed-out `ffmpeg` frame-extraction process was left running
  as an orphan/zombie.** `extract_frames()` awaited
  `proc.communicate()` under `asyncio.wait_for(..., timeout=30)` but on
  timeout just returned `[]` without touching the still-running child
  process. It now calls `proc.kill()` and awaits `proc.wait()` before
  returning.
- **Fix: a `ZipFile` handle kept open across an entire month's clip batch
  in `archiver.py` risked losing the whole archive on a crash mid-batch**
  (the central directory is only written on `close()`). `_archive_month()`
  now opens and closes the archive once per clip, so a crash can lose at
  most the one clip in flight instead of the month's whole archive.
- **Fix: `app.py`'s `_shutdown()` could skip later cleanup steps (closing
  the database, persisting tracker state) if an earlier step raised.**
  Each shutdown step now runs in its own try/except via a `_shutdown_step`
  helper, so one failing step (e.g. `analyzer.close()`) no longer prevents
  the database from closing or the tracker from saving.
- **Fix: `ON DELETE CASCADE` constraints in the clip database schema were
  silently inert.** SQLite requires `PRAGMA foreign_keys=ON` per
  connection; `database.py` never set it, so deleting a clip left orphaned
  rows in its analysis tables instead of cascading. The pragma is now set
  immediately after connecting.
- **Fix: `tracker.json` and the Blink `auth_credentials.json` file could
  be left truncated/corrupt if the add-on crashed mid-write.** Both are
  now written to a temp file and moved into place with `os.replace()`
  (atomic on the same filesystem) instead of being written in place.
- **Fix: `ClipTracker`'s downloaded-ID pruning could drop an arbitrary
  subset of IDs instead of the oldest ones.** `_downloaded` was a
  `set[str]`; CPython set iteration order is a hash-table artifact, not
  insertion order, so `_prune_if_needed()`'s "keep the last N" slicing
  wasn't actually keeping the most recently downloaded IDs. It's now an
  insertion-ordered `dict[str, None]`, so pruning genuinely drops the
  oldest entries.
- **Fix: clips beyond `max_clips_per_poll` could be permanently skipped
  instead of picked up on a later poll.** `download_new_clips()` advanced
  the tracker's `since` cursor to "now" on every poll regardless of
  whether the per-poll cap left clips undownloaded; because Blink's API is
  filtered by `since=`, any clip behind that advanced cursor was never
  fetched again. When a poll leaves a backlog, the cursor is now held back
  at the pre-poll `since` value instead of advancing.
- **Fix: `_stream_to_file()` didn't retry on request timeout and left a
  partial file behind on failure.** It only caught `aiohttp.ClientError`,
  but a `ClientTimeout` expiry raises a bare `asyncio.TimeoutError`, which
  went unhandled; a non-200 response also returned immediately instead of
  retrying like other failures. It now catches both exception types,
  retries non-200 responses with the same backoff as exceptions, and
  unlinks the partial destination file before retrying.
- **Fix: `_generate_thumbnail()`'s `ffmpeg` subprocess had no timeout** and
  could hang indefinitely on a corrupt clip. `proc.wait()` is now wrapped
  in a 30s `asyncio.wait_for()`; on timeout the process is killed and
  reaped before returning failure.
- **Fix: files deleted by the storage retention policy left orphaned rows
  in the clip database.** `apply_retention_policy()` only touched the
  filesystem with no awareness of `ClipDatabase`. A new
  `apply_retention_policy_paths()` returns the deleted clip paths, and
  `app.py`'s poll cycle now removes the matching database row for each one
  when `enable_library_db` is set.

## 3.1.1

### Bug fixes

- **Fix: `moondream_local` silently sent camera frames to Moondream Cloud,
  unauthenticated, instead of running on-device.** The `moondream` PyPI
  package's local-inference architecture changed considerably since this
  provider was written — a `moondream>=0.0.5` version pin that once made
  `md.vl(model=...)` load an on-device CPU model was later dropped in favor
  of an unpinned install, and current versions of the package default to
  `CloudVL` (the hosted API) whenever the now-required `local=True` flag is
  omitted. Because this provider never passed that flag, every detect/
  caption/query call was silently routed to `api.moondream.ai` with no API
  key attached — clips were leaving the device despite `moondream_local`
  being chosen specifically to avoid that, and every call failed
  unauthenticated (HTTP 401), so analysis silently produced empty results
  while `health_check()` still reported the provider as ready. `local=True`
  is now passed explicitly, so hosts without a supported GPU fail loudly and
  cleanly (`_ensure_model()` correctly reports the provider as unavailable)
  instead of silently degrading into a cloud data leak. The `moondream`
  install (Dockerfile build step and the AI tab's **Install** button) is now
  pinned to `>=1.3,<2` instead of unpinned, so a future upstream rewrite
  can't silently break this provider again. See `DOCS.md`'s Moondream Local
  section for the current on-device hardware requirement (CUDA or Apple
  Silicon GPU — pure-CPU inference is no longer offered by the package).
- **Fix: long clips (>30s) were under-sampled relative to short ones.**
  `_target_frame_count()` previously added a flat `+2` bonus frames for
  clips over the 30s threshold; a 55-60s clip now covers roughly twice the
  timeline of a 30s clip, so it gets its full configured frame budget
  **doubled** instead, keeping sampling density roughly constant from a
  10s clip up to Blink's 60s ceiling. The frame budget is now sized from the
  clip's real duration (from Blink API metadata) when the caller has it,
  rather than only estimating from the extracted frame count — plumbed
  through from `AnalysisQueue` and the AI tab's **Analyze Now**/**Test**
  buttons in the web UI.
- **Fix: a low-confidence "suspicious" hedge could permanently block the
  scene baseline from learning a new normal.** The visual scene-baseline
  ("smart brain") feature only folded non-suspicious clips into the learned
  background, so a persistent-but-benign change (a car parked overnight, a
  trash can put out for collection) that the model flagged out of caution
  — often because the scene-deviation hint itself nudged it to "look
  closer" — would never get absorbed into "normal," causing the same hint
  and the same hedge to keep firing on every future clip indefinitely. Only
  a *confident* suspicious verdict (confidence ≥ 0.5) now withholds a clip's
  frame from the baseline; a genuine intruder is still never absorbed into
  what's normal for that camera.
- **Fix: some AI notifications contained wall-of-text, repetitive
  descriptions instead of the one-or-two-sentence summary the analysis
  prompt asks for.** Small vision models (chiefly Moondream) occasionally
  ignore the prompt's length instructions and fall into a degenerate
  repetition loop instead of stopping — e.g. repeating "the person is
  standing near the car's rear ___" once per body part in view, or
  answering with a list of nearly every sentence it could think of. Because
  that text arrives as valid JSON in the `description` field, it previously
  passed through completely uncapped — only the non-JSON keyword-matching
  fallback path had a length limit. `parse_response()` now runs every
  description (JSON or fallback) through a new `_clean_summary()` step that
  keeps at most the first two sentences, cuts off immediately at the first
  sentence that repeats one already kept, and falls back to a hard
  character cap for text with no sentence punctuation to split on at all.
- **Fix: restarting the add-on could knock Home Assistant's own Blink
  integration offline (camera battery levels etc. showing "unavailable"
  until that integration was manually reloaded).** `disconnect()` — run on
  every graceful shutdown, including ordinary Supervisor restarts — called
  Blink's `/client/{id}/logout` endpoint, revoking this add-on's own auth
  token server-side every time it stopped. That defeated the token caching
  `connect()` is built around (`AUTH_FILE`): the next start would find its
  cached token already revoked and fall back to a full username/password
  OAuth login instead of a quiet token-based reconnect. A full re-login is
  an account-wide auth event that can transiently invalidate other
  sessions on the same Blink account, including Home Assistant's own Blink
  integration. `disconnect()` no longer calls Blink's logout endpoint at
  all — it only closes this add-on's local HTTP session, leaving the
  persisted token valid server-side so restarts reconnect quietly and
  never disturb any other client authenticated on the same account.

## 3.1.0

### Features

- **New "Frames Analyzed" status card.** The Status page now shows a card
  underneath Storage with the total number of frames/images the AI has
  analyzed, and how many were analyzed today. This is tracked separately
  from clip counts since each clip's AI analysis inspects multiple frames.

### Improvements

- **Shorter, cheaper, still-accurate AI descriptions.** 3.0.8's richer
  Moondream pipeline improved detection accuracy but also made descriptions
  noticeably more verbose — narrating background scenery (other parked
  vehicles, utility poles, power lines, foliage, general neighborhood
  description) that adds nothing to a security summary while driving up
  completion tokens. Descriptions are now capped at one sentence (two only
  when genuinely necessary), scoped strictly to the notable person, vehicle,
  or animal and what they're doing, e.g. "A person is walking past the car."
  Static background scenery is explicitly excluded. Moondream Cloud/local
  grounding captions now request `length="short"` instead of `"normal"`,
  avoiding the exhaustive scene inventory that was leaking into final
  descriptions. The detect-augmented accuracy pipeline (person/vehicle/animal
  detection, proximity hints, reasoning) is unchanged — only the verbosity of
  the final text and the grounding caption's detail level were reduced.

### Bug fixes

- **Fix: Status/AI Usage/Automations/AI pages could be forced up to ~3x
  wider than the mobile viewport, clipping content with no way to scroll
  back.** `.page.active{display:flex}` makes `.status-grid` and
  `.auto-content` flex items, which default to `min-width:auto` — so
  content with a large intrinsic width (the 7-day activity chart, or the
  Automations event table's long unbreakable `sensor.blink_downloader_status`
  / `blink_clip_downloaded` identifiers) silently forced the whole page
  hundreds of pixels past the screen edge, and `body{overflow:hidden}`
  clipped the overflow with no scroll gesture able to reach it. Both
  containers now get `min-width:0` so they properly shrink to the viewport,
  and the two data tables (Automations event table, AI Usage per-model
  breakdown) are wrapped in a dedicated horizontally-scrollable container so
  content that genuinely can't shrink further (like those identifiers)
  scrolls locally instead of blowing out the page. Desktop layout is
  unchanged.

### Notes

- Verified the AI tab's Queue Status card (Pending/Processing/Completed/
  Failed) already counts one entry per clip, not per frame — the
  `analysis_queue` table enforces a unique row per clip and the analyzer
  always returns a single result per clip regardless of how many frames it
  inspects internally. No change was needed there.

## 3.0.10

### Bug fixes

- **Fix: restoring an HA backup could trigger a wave of AI re-analysis,
  burning tokens.** When a backup restore rolls `/data` (the download
  tracker and clip database) back to an older snapshot while `/share` keeps
  the newer clip files, the next poll cycle re-fetches those clips from
  Blink, finds them already on disk, and re-links them into the tracker
  instead of re-downloading. Those re-linked clips were still being routed
  through the same pipeline as freshly-downloaded ones, including
  notifications, webhooks, and — critically — the AI analysis queue,
  silently re-running (and re-billing) analysis for clips that had likely
  already been analyzed before the restore. Re-linked clips are no longer
  treated as new downloads.
- **Fix: legacy Moondream Cloud rows split the Per-Model Breakdown into two
  "models".** Pre-3.0 analyses were stored under the provider name
  (`moondream-cloud` / `moondream_cloud`) instead of the actual model ID, and
  predate per-request token tracking, so they showed up as a second entry
  stuck at 0 tokens next to `moondream3-preview`. Those rows are now
  normalized to `moondream3-preview` on startup so usage stats reflect one
  real model.

## 3.0.9

### Bug fixes

- **Fix: "processing" queue items stuck forever after a restart.** When the
  add-on was restarted or crashed while analyzing a clip, those items were
  left with `status='processing'` in the database and never retried — the
  queue only picks up `status='pending'` entries. On startup the database now
  resets any stale processing items back to pending so they are retried on
  the next analysis cycle. This was the cause of the perpetual "X processing"
  count in the AI tab even with no active analysis.
- **Fix: missed car-proximity alerts when Moondream detect fails to find the
  car.** 3.0.8 introduced an explicit "The protected vehicle is not visible
  in this frame" hint whenever a person was detected but the `/detect car`
  call returned nothing. This actively suppressed the base prompt's
  vehicle-distance rules even when the car was genuinely in frame but the
  detect call happened to miss it — resulting in no alert despite the person
  being right next to the vehicle. The suppression hint is removed; the base
  prompt's rules ("never apply vehicle-distance rules unless a vehicle is
  genuinely visible in frame") remain in effect and let the model make its
  own judgment from the visual evidence. Applies to both `moondream_cloud`
  and `moondream_local`.

## 3.0.8

### Moondream — accurate captions on every camera, fewer false positives

- **Fix: non-car cameras stopped producing captions.** Animal detection was
  only run when a protected vehicle applied to the camera, and non-car
  cameras had no vehicle detection at all — so a camera with no person in
  frame (e.g. a cat crossing the yard, or a car simply passing by) always
  hit the hardcoded "no subject detected" skip response instead of reaching
  the model, silently suppressing captions for every camera except the one
  watching the protected vehicle. Animal detection now always runs, and
  non-car cameras also run a generic vehicle detect, so any person, animal,
  or vehicle visible on any camera reaches captioning.
- **Fix false "very close to"/"right next to the car" alerts for ordinary
  passing traffic.** Two vehicle bounding boxes can appear close or even
  touching in a single 2D frame while being many feet apart in real depth —
  a car driving past on the street routinely overlaps a parked car's box in
  screen space purely from camera perspective. The proximity hint shared
  with person/animal detection (where 2D distance is a reasonable proxy,
  since they share the vehicle's ground plane) was being applied to
  vehicle-to-vehicle proximity too, and instructed the model to parrot
  phrases like "right next to the car" from bounding-box gap alone. A
  dedicated, more conservative `_vehicle_proximity_hint` now handles
  vehicle-to-vehicle cases: it never suggests proximity language and only
  allows `suspicious=true` when the frames themselves show the other
  vehicle actually stopping, parking, or backing up close to the protected
  vehicle — ordinary through-traffic is described plainly (e.g. "a car
  drove up the street") with `suspicious=false`.
- Non-car cameras now inject a labeled position hint (Person/Animal/Vehicle)
  built from whichever subjects are actually detected on that camera, so
  each camera's caption reflects only what its own frames show — no
  borrowed car/driveway language from a different camera's perspective.
- Person/animal proximity rules (touching, within 1 foot, 1–3 feet, several
  feet) are unchanged: contact with or lingering within a foot of the
  protected vehicle still reliably produces `suspicious=true` at
  confidence ≥0.8.
- Applies identically to `moondream_cloud` and `moondream_local` — both
  share the same detection/hint pipeline via `_MoondreamDetectionMixin`.

## 3.0.7

### Dependencies

- **blinkpy** stays at `>=0.25.7` (already current).
- **aiohttp** bumped from `>=3.11.0` to `>=3.14.1`.
- **aiofiles** bumped from `>=24.1.0` to `>=25.1.0`.
- **aiosqlite** bumped from `>=0.21.0` to `>=0.22.1`.
- **aiosmtplib** bumped from `>=3.0.0` to `>=5.1.2`.
- **Pillow** bumped from `>=11.0.0` to `>=12.3.0`.
- **anthropic** bumped from `>=0.100.0` to `>=0.115.1`.
- **openai** bumped from `>=1.0.0` to `>=2.44.0` — note this crosses the
  OpenAI SDK's v1 → v2 major version boundary; `OpenAIAnalyzer` was verified
  against 2.x and required no code changes.
- Test-only dependencies bumped: **pytest** `>=8.0` → `>=9.1.1`, **pytest-asyncio**
  `>=0.23` → `>=1.4.0`, **pytest-cov** `>=5.0` → `>=7.1.0`, **aioresponses**
  `>=0.7.6` → `>=0.7.9`.
- `requirements.txt` (the Dockerfile's pre-install layer) now also lists
  **anthropic** and **openai** directly, instead of only picking them up via
  the later `pip install /app` step — keeps the Docker layer cache warm when
  only the app package changes.

If any of these updates cause problems, pin back to the 3.0.6 add-on version
to roll back to the prior dependency floors.

## 3.0.6

### AI analysis — world-class accuracy pass

- **Fix custom prompts silently reverting after being cleared in the UI.**
  The car-camera fix in 3.0.5 made `is_car_camera` a true full replace on the
  live analyzer, but per-camera `custom_prompt` was still merged with
  `dict.update()` — clearing a camera's custom prompt in the AI tab saved the
  change to `camera_configs.json` correctly, but the running analyzer kept
  using the last non-empty prompt until the add-on restarted. Descriptions,
  custom prompts, and car-camera flags are now all pushed to the live
  analyzer through dedicated `update_camera_prompts()` / `update_car_cameras()`
  methods that fully replace their mapping, matching the full-replace
  contract `update_camera_descriptions()` already had.
- **Sharpen car-camera distance rules to cut false positives on people
  passing at a distance.** The protected-vehicle rules now key off
  *lingering or contact* versus *simply passing through* rather than raw
  distance alone: someone or something walking, running, or driving past
  without stopping is never flagged regardless of how close their path
  happens to run, while touching/reaching/circling the vehicle — or an
  animal investigating it, or another vehicle stopping close beside it —
  is. The "not suspicious" distance floor also tightened from 5 feet to 3
  feet so genuinely nearby lingering isn't waved through.
- **Fix Moondream Cloud car cameras silently missing animals and second
  vehicles.** The detect-augmented fast path short-circuited to "not
  suspicious" for any frame where `/detect` found zero people — even on a
  car camera where a dog sniffing at the car, or another car stopping right
  beside it, should have been caught. Car-camera frames with no person now
  also check for an animal or a second vehicle near the protected car before
  being written off, and inject the same proximity-hint language used for
  people so the model still gets an evidence-based distance estimate.
- **Scene-baseline "smart brain" now refreshes after a persistent change.**
  The per-camera visual baseline learned in 3.0.5 used a slow exponential
  average (floor alpha 0.05) that could take 45+ clips to catch up after
  something was genuinely added to or removed from a camera's view. Once a
  baseline is established, 5 consecutive ordinary (non-suspicious) clips in
  a row showing elevated deviation are now treated as a real, lasting scene
  change and snap the baseline toward the new normal in one fast blend,
  instead of waiting dozens of samples for the slow steady-state average to
  catch up. A one-off flicker that doesn't repeat 5 times running still
  decays back to the slow EMA as before.
- **Strengthen how per-camera descriptions steer suspicion.** The camera
  "location and purpose" text configured in the AI tab now explicitly
  instructs the model to use that stated purpose to calibrate what counts as
  routine versus worth scrutiny for that specific camera, instead of relying
  on the model to infer scope from the description alone.

## 3.0.5

### AI analysis — full-clip frame coverage

- **Fix frame extraction only sampling the first few seconds of a clip.**
  Blink motion clips can run up to 60 seconds, but `extract_frames` requested
  exactly `max_frames` (or `2 × max_frames` in `smart` mode) frames from
  ffmpeg at the configured `frame_interval` — with the defaults (5 frames,
  2s interval) that's only the first 10–20 seconds. Anything that happened
  later in a longer clip, including the actual point of interest, was never
  extracted or seen by the AI. Extraction now always requests enough frames
  to cover a full 60-second clip regardless of strategy or `max_frames`;
  ffmpeg naturally emits fewer frames for shorter clips, so this only ever
  adds coverage, never cost.
- **`sequential` and `uniform` strategies now down-select their oversampled
  pool.** Since extraction now oversamples to cover the whole clip, all three
  frame strategies need a down-selection step before frames are sent to the
  AI, or the two non-`smart` strategies would send far more frames (and API
  calls) per clip than `max_frames` configures. `smart` and `sequential` both
  use the existing motion-weighted entry/peak/exit picker; the new
  `_select_uniform_frames` evenly spaces `uniform`'s picks across the whole
  extracted pool, preserving its "no motion analysis" contract while fixing
  its coverage.
- **Fix car-camera protection silently reverting after being disabled in the
  UI.** Unchecking every "protected vehicle visible from this camera" box in
  the AI tab persisted the change to `camera_configs.json` correctly, but the
  live analyzer's in-memory car-camera set was only updated when the new
  selection was non-empty — so a running add-on kept applying car-proximity
  rules to a camera until the next restart. `camera_configs.json` is the
  documented single source of truth for `is_car_camera`, so the live analyzer
  now always adopts the saved selection, including clearing it.
- **Broaden car-proximity prompt language to "anyone or anything."** The
  protected-vehicle distance rules only described people approaching the
  vehicle. They now also cover animals and other vehicles stopping, parking,
  or backing in close to the protected vehicle, while keeping the existing
  false-positive guidance that ordinary through-traffic and pedestrians who
  don't stop near the vehicle are not suspicious.

### Configuration — camera name fields

- **Clarify how `ai_camera_prompts`, `ai_camera_descriptions`, and
  `ai_car_cameras` camera names must be entered.** These add-on options are
  free-text fields in the Supervisor's config form — a platform limitation
  of the schema-driven options UI, which can't render a live dropdown of your
  actual Blink camera names. It wasn't clear whether the value you typed had
  to match the camera's exact name in Blink. The option descriptions now say
  explicitly that the name must exactly match your Blink camera name
  (case-sensitive), and point at the add-on's own web UI (**AI tab → Camera
  Configurations**) as the easier way to configure these per camera: it lists
  your actual cameras and lets you set the description, custom prompt, and
  car-protection flag directly against them, with no typing or guesswork.

### AI analysis — smarter frame budget and selection

- **Long clips now get a few bonus frames instead of stretching the same
  budget thinner.** `ai_max_frames`/`ai_frame_interval` are sized around a
  typical short motion clip; applying them unchanged to a clip estimated at
  over 30 seconds spread the same handful of frames across twice the footage,
  risking gaps in the AI's view of what happened. Clips estimated at 30
  seconds or less still use exactly the configured `max_frames`; anything
  longer now gets `max_frames + 2`, a small enough bump to keep token usage
  in check while giving longer clips a real chance at full coverage.
- **`smart`/`sequential` frame selection now spreads its picks across the
  whole clip instead of clustering around a single motion burst.** Previously
  the down-selector picked the single highest-motion frame plus whichever
  other frames had the next-highest motion-diff scores — for a clip with one
  concentrated burst of motion (e.g. a car passing through frame), most or
  all of the "extra" picks beyond first/peak/last could bunch up around that
  same moment, leaving the rest of the clip unrepresented. Frame selection
  now enforces a minimum spacing between selected frames so picks are spread
  across the timeline, falling back to the old unconstrained behavior only
  when the raw frame pool is too small to space picks out.

### AI analysis — visual scene baseline ("smart brain")

- **New: per-camera visual scene baseline learns what a camera's background
  normally looks like.** Blink cameras are stationary, so a given camera's
  clips almost always frame the same porch, driveway, or yard. Building on
  the existing time-of-day/frequency "Smart Security Brain" anomaly score,
  the analyzer now also reduces each clip's opening frame to a small
  grayscale thumbnail and blends it into a running per-camera baseline
  (`camera_scene_baselines` table). Once a camera has enough history (20
  clips), each new clip is compared against its baseline; a frame that looks
  like the camera's normal, empty background reinforces a calm read of the
  activity, while a frame that looks visually different from the norm (a
  package, a parked vehicle, an obstruction) is flagged in the prompt as
  worth a closer look. The baseline only learns from clips the AI did not
  flag as suspicious, so a genuine intruder or anomaly can't teach the
  system to treat itself as normal.

### AI analysis — reduced false positives, more professional tone

- **The AI now explicitly classifies people, vehicles, and animals before
  describing them**, instead of leaving subject identification implicit —
  reducing cases where a passing car's motion got described in
  person-oriented language or vice versa.
- **Ordinary passing traffic is now described as just that.** A car that
  drives up or down the street without stopping, parking, or slowing near a
  person or entryway is described plainly (e.g. "a car drove up the
  street"), never as being "near" the protected vehicle, a person, or
  anything else in frame — that language is now reserved for cases where a
  person, vehicle, or animal actually stops or lingers close to something.
  This applies both generally and to the protected-vehicle distance rules.
- **Refined tone: calm, factual, professional security-analyst language
  throughout.** The prompt now explicitly asks the model to state only what
  is observable, avoid speculation or alarmist wording, and reserve
  "suspicious" for genuine cause for concern, aiming to cut down on
  false-positive alerts from routine activity described in dramatic terms.

## 3.0.4

### Bug fixes

- **Fix car/driveway language leaking onto cameras that can't see the car.**
  Leaving the AI tab's "car camera" checkboxes unset for a camera makes
  `car_cameras` empty, which is documented as "applies to all cameras" — but
  the OUTPUT RULES block in the analysis prompt unconditionally seeded the
  model with a car/driveway example phrase regardless of which camera actually
  took the clip. A camera like Front Door, which has no vehicle in its field
  of view, could still get car-distance language in its description. The
  prompt now states explicitly when a camera does not view the protected
  vehicle, and only uses the car-distance example phrase for cameras where the
  vehicle distance rules actually apply.
- **Fix mobile web UI cut-off elements.** On narrow Android viewports the top
  navigation bar didn't wrap, so the rightmost buttons (Refresh/Sync) were
  clipped by the page's `overflow:hidden`. The nav now wraps onto a second row
  below 600px width, the toast notification no longer risks spilling past the
  left edge on very narrow screens, and grid layouts (clip grid, status cards,
  usage cards, AI cards) now shrink below their minimum column width instead
  of overflowing on viewports narrower than that minimum.

## 3.0.3

### Bug fixes

- **Fix local-storage sync crash.** The Blink API returns `size` as a string in
  the local-storage manifest.  Dividing it by `1024` for the log message raised
  `unsupported operand type(s) for /: 'str' and 'int'`, which caused every
  local-storage clip download to fail silently.  The value is now cast to `int`
  before the division.
- **Widen 2FA verification modal.** The "Verify" button was partially cut off at
  `max-width: 420px`; bumped to `480px` so the button is always fully visible.

### Dependencies

- Bumped minimum **blinkpy** requirement to `>=0.25.7`.  The 0.25.7 release
  initialises `response_text = ""` to prevent an `UnboundLocalError` on
  unexpected status codes in the OAuth login flow.

## 3.0.2

### Moondream Cloud — fine-tuning support

- **`MoondreamFineTuneManager` class.** A complete HTTP API wrapper for the
  Moondream Cloud fine-tuning API, enabling programmatic management of
  fine-tuning workflows entirely in the cloud (no local GPU required).
  Supported operations:
  - `create_finetune(name, rank)` — create a LoRA fine-tune (rank 8/16/24/32)
  - `list_finetunes()` / `get_finetune(id)` / `delete_finetune(id)` — manage
    fine-tune lifecycle
  - `generate_rollouts(finetune_id, image, question, num_rollouts, ...)` —
    generate multiple model outputs for scoring; supports `query`, `point`, and
    `detect` skills; automatic reward computation via `ground_truth`
  - `train_step(finetune_id, request, rollouts, rewards, mode, lr)` — execute
    one RL (reinforcement learning) or SFT (supervised fine-tuning) training
    step
  - `save_checkpoint(id)` / `list_checkpoints(id)` / `delete_checkpoint(id, step)`
    — checkpoint management
  - `log_metrics(id, step, metrics)` — record custom evaluation metrics
  - `get_model_id(finetune_id, step)` — static method that returns the
    inference model identifier (`moondream3-preview/{id}@{step}`) for use
    with `MoondreamCloudAnalyzer`

- **Fine-tuned model inference in `MoondreamCloudAnalyzer`.**  A new
  `finetune_model` parameter (also exposed in `create_analyzer()` as
  `moondream_finetune_model`) routes all `/query` and `/detect` requests to a
  custom fine-tuned checkpoint instead of the base `moondream3-preview` model.
  Build the value with `MoondreamFineTuneManager.get_model_id()` after
  training.  `model_name()` and `fetch_models()` both reflect the fine-tuned
  model when it is configured.

## 3.0.1

### AI analysis — improved responses and prompts

- **Fixed: internal spatial data leaking into descriptions.** AI responses were
  including raw technical terms like "bounding box overlaps", "normalised gap
  0.021", and "18.4% of the frame width" verbatim in the user-visible
  description.  All SPATIAL DATA context injected for Moondream Cloud is now
  labelled as `[INTERNAL PROXIMITY HINT — use for reasoning only]` so models
  treat it as internal context and write plain-English descriptions instead.

- **Enhanced prompt with explicit plain-language rule.** Every prompt now ends
  with an `OUTPUT RULES` section instructing the AI to write the description
  in natural language ("about 2 feet from the car") and never quote technical
  terms such as "bounding box", "normalized", "frame percentage", or "spatial
  data".

- **Anthropic: system prompt now used.** Claude models receive a dedicated
  `system` prompt separating role/format instructions from user content,
  improving JSON compliance and preventing internal analysis terms from
  appearing in descriptions.  `max_tokens` reduced to 512 (sufficient for
  a short JSON response).

- **OpenAI: system message + JSON object mode.** GPT-4o, GPT-4.1, and
  GPT-4-Turbo models now receive a system message with role and output rules
  and use `response_format: json_object` to guarantee valid JSON output.
  Image detail bumped from `"auto"` to `"high"` for better scene analysis.
  `max_tokens` reduced to 512.

- **Ollama: JSON format mode + system prompt.** The `/api/generate` payload
  now includes `"format": "json"` (forces valid JSON output on Ollama ≥ 0.1.9)
  and a `"system"` field with role and output rules.

- **Improved default AI prompt.** The built-in base prompt in `config.yaml`
  and `config.py` is rewritten as a clear step-by-step instruction that
  emphasises natural-language output and avoids ambiguous phrasing.

### Camera configuration — per-camera UI improvements

- **Car-camera checkbox in the AI tab.** Each camera card in the "Camera
  Configurations" section (AI tab → Camera Configurations) now shows a
  **"Protected vehicle visible from this camera"** checkbox.  Checking it
  enables car-proximity alert rules for that camera without editing
  `config.yaml`.  The setting is saved to `/data/camera_configs.json` and
  applied immediately without restart.

- **Car cameras now loaded from the web UI on startup.** Previously the
  `ai_car_cameras` list was only read from `options.json` at boot.  On startup,
  the add-on now loads camera flags set via the web UI (`is_car_camera: true`
  entries in `camera_configs.json`) and uses those as the authoritative source;
  `options.json` is kept as a backward-compatible fallback.

- **Custom prompts from the web UI now survive restarts.** Per-camera custom
  prompts set via the Camera Configurations section were only applied when saved
  and were lost on add-on restart.  They are now loaded from
  `camera_configs.json` on startup alongside descriptions and car-camera flags.

- **Single source of truth for per-camera settings.** `camera_configs.json`
  (editable via the AI tab web UI) is now the primary source for descriptions,
  custom prompts, and car-camera flags.  `ai_camera_descriptions` and
  `ai_camera_prompts` in `options.json` remain as fallbacks for cameras not
  yet configured in the web UI.

## 3.0.0

### New features — Smart Security Brain

- **Behavior memory baseline.** The add-on now records per-camera hourly event
  frequency and average clip duration in SQLite every time a clip is downloaded.
  This data is used to compute an *anomaly score* (0.0–1.0) for every clip that
  goes through AI analysis, answering: "Is this event unusual for this camera at
  this time of day?"

- **Anomaly-aware AI prompts.** When a clip scores ≥ 0.6 on the anomaly scale
  the prompt automatically includes a **BEHAVIOR ALERT** flag with the score,
  telling the model to apply heightened scrutiny.  After ~30 events per camera
  the system activates; before that it stays silent to avoid false positives on
  new installs.

- **Time-of-day context in every prompt.** Every AI call now includes the
  clip's local time label ("early morning", "evening", "night", etc.) derived
  from the clip timestamp, so the model can calibrate what constitutes
  suspicious behaviour for that time.

- **Smart frame selection (new default).** `ai_frame_strategy: "smart"` (new
  default) extracts 2× `ai_max_frames` candidates then uses PIL inter-frame
  motion-diff to select the *entry* frame, *peak-motion* frame, and *exit*
  frame.  This delivers better coverage of the full event with the same or
  fewer frames sent to the AI.

- **Sequential per-frame analysis mode.** `ai_frame_strategy: "sequential"`
  analyses each frame individually via separate AI calls and returns the most
  alarming result (suspicious > non-suspicious; higher confidence when tied).
  This mode works for **all six providers** and produces sharper results when
  the AI performs better on individual images than on batches.

- **`anomaly_score` stored on every analysis result.** The computed anomaly
  score is now persisted in `analysis_results.anomaly_score` and included in
  every `AnalysisResult` dict returned by the API, so you can query and filter
  by it.

- **Reduced frame size: 640 px width.** The ffmpeg extraction command now uses
  `scale=640:-1` (down from 1280) — the single biggest per-frame token cost
  reduction.  Frame selection via smart mode offsets any quality loss by
  ensuring only the most informative frames are sent.

- **`ai_frame_strategy` config option.** New `options` key with three choices:
  `"smart"` (default), `"sequential"`, or `"uniform"` (legacy behaviour).
  Documented in `config.yaml` and exposed in the schema.

- **Camera-scoped car-protection rules (`ai_car_cameras`).** New config key
  accepts a list of camera names.  When set, the PROTECTED VEHICLE distance
  rules are only injected into prompts for those cameras.  Cameras not in the
  list use only their own description, preventing false positives like
  "person near the vehicle" on cameras that can't see the car.  Leave empty
  (default) for backward-compatible all-camera behaviour.

- **Modal AI panel shown immediately on page load.** Previously the AI analysis
  header would not appear in the clip modal until the user visited the AI tab,
  because the enabled-state check was deferred.  AI status is now fetched at
  boot so the panel header shows up on the very first clip the user opens.

- **Moondream Cloud: reasoning mode enabled.** All `/query` requests now
  include `"reasoning": true`, enabling multi-step spatial analysis (proximity
  estimates, evasive behaviour detection).  Per Moondream docs this adds
  10-20 % latency with no extra cost.

- **Moondream Cloud: estimated token tracking.** The Moondream API does not
  return usage statistics.  The add-on now accumulates *estimates*
  (256 image tokens + prompt text tokens per frame, plus answer text tokens
  for completion) so the AI usage table shows approximate figures instead
  of N/A.  A note in the UI clarifies these are estimates.

- **OpenAI token counts now shown in usage table.** The UI was erroneously
  hiding token stats for the OpenAI provider even though the API returns
  exact counts.  Fixed.

## 2.9.0

### New features

- **Multi-frame analysis for Moondream Cloud and Local.** Previously only the
  middle frame of a clip was sent to Moondream. Now every extracted frame is
  analysed individually and the most alarming result (suspicious over
  not-suspicious; higher confidence when tied) is returned. The Moondream Cloud
  path respects the 2 req/s rate limit with a 0.55 s inter-frame delay.

- **Improved AI prompt with distance estimates.** The default `ai_prompt` and
  the car-proximity block now ask the model to explicitly estimate the distance
  between any person and a protected vehicle (e.g. "approximately 1.5 feet from
  the driver-side door"). Strict distance thresholds (1 ft, 2 ft, 5 ft) are
  enforced: `suspicious=true` is only set when the person is genuinely close.

- **Raised `ai_max_frames` cap to 100.** The default increased from 3 to 5 and
  the maximum allowed value increased from 10 to 100, enabling thorough analysis
  of longer clips.

- **Per-camera descriptions (`ai_camera_descriptions`).** Each camera can now
  have a plain-text description of its location and purpose that is included in
  the AI prompt. Descriptions can be set in `options.json` via
  `ai_camera_descriptions` or at runtime through the new Camera Configurations
  panel in the web UI.

- **Camera Configurations panel in the AI tab.** A new section in the web UI
  lists every known camera and lets you type a description per camera.
  Changes are saved to `/data/camera_configs.json` and take effect immediately
  without restarting the add-on.

- **Improved frame extraction quality.** Frames are now extracted at
  `scale=1280:-1` and JPEG quality `-q:v 2` (up from `-q:v 5`), providing
  sharper images for AI analysis.

- **Descriptive config comments.** Every AI-related setting in `config.yaml`
  now includes an explanatory comment describing what it does and its valid
  range.

## 2.8.8

### Bug fixes

- **Account lockout prevention.** When Blink rejects invalid credentials the
  add-on now stops retrying immediately and logs a clear "fix username/password
  and restart" message.  Previously it retried indefinitely with the same bad
  credentials, which could trigger Blink's fraud-detection lockout.  Transient
  network errors and 2FA timeouts are still retried as before.

- **Moondream Cloud: confidence always 0.0 in Discord.** Small models like
  Moondream often return `"confidence": 0.0` even when flagging suspicious
  activity because they don't calibrate confidence scores.  The response parser
  now detects this case and derives a non-zero confidence via keyword matching
  (defaulting to 0.5 when no keywords match), so Discord embeds and alerts show
  a meaningful value instead of "0%".

- **Moondream Cloud pricing corrected.** Fetch-models now reports the correct
  Moondream Cloud pricing: $0.30/$2.50 per 1M input/output tokens, sourced from
  https://docs.moondream.ai/pricing/.

### New features

- **Silent no-suspicious-activity logs.** Analysis results that are not
  suspicious are now logged at DEBUG level instead of INFO, reducing noise.
  Suspicious results continue to log at INFO with the confidence score and a
  summary snippet.

- **Minimum confidence threshold (`ai_min_confidence`).** Set this to e.g.
  `0.3` to suppress notifications for low-confidence detections.  Clips are
  still analysed and stored; only the alert dispatch is gated.  Default is
  `0.0` (no change in behaviour).

- **Per-camera AI prompts (`ai_camera_prompts`).** Cameras that do not point
  directly at the car can now use a different prompt from the one used for the
  driveway/car camera.  Each entry is `{camera: "Camera Name", prompt: "..."}`;
  cameras not listed fall back to `ai_prompt`.

- **Improved car-proximity detection.** When `ai_car_description` is set the
  prompt now explicitly instructs the AI to flag as suspicious anyone who comes
  within 1-2 feet of the vehicle, even when their intent is unclear.  Previously
  the instruction only asked to "pay special attention" to the car, which missed
  close-proximity events.

- **Updated default `ai_prompt`.** The built-in prompt now requests a minimum
  confidence of 0.1 (instead of 0.0) and adds the phrase "Respond ONLY in JSON"
  to reduce the chance of the model including extra text that breaks JSON parsing.

## 2.8.7

### New features

- **OpenAI AI provider.** A sixth AI provider is now available.
  Set `ai_provider: openai` and supply your `openai_api_key` from
  [platform.openai.com](https://platform.openai.com).  Any OpenAI vision model
  is supported (GPT-4o, GPT-4.1, GPT-4-Turbo, and variants) — use the **Fetch
  Models** button in the AI tab to browse models pulled live from the OpenAI API.
  `gpt-4o-mini` is selected by default as the most cost-effective option.

- **Model selector for OpenAI.** The model-picker dropdown is shown when the
  OpenAI provider is selected.  Clicking **Fetch Models** lists all available
  vision-capable GPT models with their pricing so you can pick the right balance
  of quality and cost.

- **Estimated API cost for OpenAI.** The AI Usage tab shows an **Estimated Cost**
  stat when the OpenAI provider is active and token data is available.  Cost is
  calculated from cumulative prompt and completion token counts multiplied by the
  model's per-token rate.

- **Clear error messages for invalid OpenAI credentials.** If an OpenAI API key
  is invalid or lacks permission, the add-on logs `AuthenticationError` and
  `PermissionDeniedError` messages that say exactly what went wrong and direct the
  user to check `openai_api_key` in the add-on settings.

- **OpenAI: client-side frame resizing.** JPEG frames are resized to a maximum of
  2048 px on the long side before being sent to the OpenAI API, matching OpenAI's
  server-side cap and reducing upload bandwidth for high-resolution cameras.

- **OpenAI: full error handling.** `RateLimitError`, `BadRequestError`,
  `APIConnectionError`, and `APIStatusError` are all caught and logged with
  clear messages, consistent with the Anthropic provider.

- **README: OpenAI setup guide.** Step-by-step setup instructions for the OpenAI
  provider have been added to the repository README alongside the existing guides
  for Ollama, Moondream, and Anthropic.

### Dependencies

- **openai** added (`>=1.0.0`) — required for the new OpenAI provider.

## 2.8.6

### New features

- **Anthropic (Claude) AI provider.** A fifth AI provider is now available.
  Set `ai_provider: anthropic` and supply your `anthropic_api_key` from
  [console.anthropic.com](https://console.anthropic.com).  Any Claude vision
  model is supported — use the **Fetch Models** button in the AI tab to browse
  models pulled live from the Anthropic API.  `claude-haiku-4-5` is selected by
  default as the most cost-effective option.

- **Model selector for Anthropic.** The model-picker dropdown (previously
  shown only for Ollama providers) is now also displayed when the Anthropic
  provider is selected.  Clicking **Fetch Models** lists all available Claude
  models with their pricing so you can pick the right balance of quality and cost.

- **Estimated API cost for Anthropic.** The AI Usage tab now shows an
  **Estimated Cost** stat when the Anthropic provider is active and token data
  is available.  Cost is calculated from cumulative prompt and completion token
  counts multiplied by the model's per-token rate, giving you a running total
  without needing to log into the Anthropic dashboard.

- **Clear error messages for invalid credentials.** If an Anthropic API key is
  invalid or lacks permission, the add-on now logs `AuthenticationError` and
  `PermissionDeniedError` messages that say exactly what went wrong and direct
  the user to check `anthropic_api_key` in the add-on settings.  Health-check
  failures for all providers already followed this pattern; Anthropic is now
  consistent with them.

- **All config options now have descriptions.** Every option in `config.yaml`
  now has an inline comment explaining what it does, including the previously
  undocumented `download_local_storage` setting (downloads clips stored on the
  USB drive attached to the Blink Sync Module — clips still transit via the
  Blink cloud even though they live on local hardware).

- **`ollama_cloud_api_key` exposed in config.yaml.** The Ollama Cloud API key
  was handled in code since v2.8.3 but was missing from the `options` and
  `schema` sections of `config.yaml`, making it invisible in the HA add-on UI.
  It is now correctly listed as a `password?` field.

### Improvements

- **Anthropic: client-side frame resizing.** JPEG frames are now resized to a
  maximum of 1568 px on the long side before being sent to the Anthropic API.
  This matches Anthropic's own server-side cap, reduces upload bandwidth for
  high-resolution cameras, and keeps image token counts predictable.

- **Anthropic: richer error handling.** `RateLimitError` (HTTP 429) now logs a
  clear "rate limit hit" warning instead of falling through to the generic
  handler.  `BadRequestError` (HTTP 400, e.g. unsupported model or image
  format) logs the API's own error message and hints to check the model setting.
  `APIConnectionError` (network failures reaching api.anthropic.com) is now
  caught and logged separately from generic exceptions.

- **yamllint configuration.** Added `.yamllint` at the repository root with a
  120-character line-length limit (up from the yamllint default of 80) so that
  `yamllint .` passes without reformatting `config.yaml`.

- **README: AI provider setup guides.** The repository README now includes
  step-by-step setup instructions for each AI provider (Ollama, Ollama Cloud,
  Moondream Cloud, Moondream Local, and Anthropic Claude), plus a Feature
  Requests section pointing to the GitHub issue tracker.

### Dependencies

- **anthropic** bumped from `>=0.50.0` to `>=0.100.0` — the 0.50 floor was
  approximately one year behind the current release.
- **aiohttp** bumped from `>=3.9.0` to `>=3.11.0`.
- **aiofiles** bumped from `>=23.2.1` to `>=24.1.0`.
- **aiosqlite** bumped from `>=0.20.0` to `>=0.21.0`.
- **python-slugify** bumped from `>=8.0.1` to `>=8.0.4`.
- **aiosmtplib** bumped from `>=2.0.0` to `>=3.0.0`.
- **Pillow** bumped from `>=10.0.0` to `>=11.0.0`.

## 2.8.5

### Bug fixes

- **Fixed spurious 2FA prompts after HA server restart with no SMS from Blink.**
  The `blinkpy_compat.py` compatibility patch replaced blinkpy's `oauth_signin`
  with a simplified version that treated *any* HTTP 202 response from Blink's
  OAuth signin endpoint as a 2FA challenge.  Blinkpy 0.25.6+ correctly inspects
  the JSON response body — only returning "2FA required" when the body contains
  `tsv_state`, `tsv_methods`, or `next_time_in_secs`.  On HA server restart,
  when the network is still stabilising, Blink's signin endpoint can return 202
  without those fields (a transient non-2FA response).  The old patch would
  immediately display the 2FA overlay even though Blink never sent a verification
  code, leaving the user with a prompt they could not complete.  After the 600 s
  timeout the add-on retried and showed the prompt again — repeating
  indefinitely.

  The patch now performs the same body inspection as blinkpy 0.25.7.  A 202
  without the 2FA indicator fields is treated as a login failure (not a 2FA
  challenge), so the add-on retries the connection and recovers once the network
  is fully up, rather than getting stuck in a 2FA loop.

- **Fixed broken info-page link in the HA add-on repository.**  The `url`
  field in `config.yaml` used a YAML block scalar (`>-`) split across two
  lines, which YAML joins with a space.  HA URL-encodes that space as `%20`,
  producing an invalid link
  (`…/tree/main/%20blink_clip_downloader`).  The field is now a plain
  quoted string pointing directly to the repository root:
  `https://github.com/brianbaggs35/ha-blink-clip-downloader`.

- **Fixed `ollama_cloud` missing from the HA add-on `ai_provider` schema.**
  The `ollama_cloud` provider was added in 2.8.3 but the `config.yaml` schema
  still listed only `ollama|moondream_cloud|moondream_local`.  Users who set
  `ai_provider: ollama_cloud` would see a configuration validation error when
  saving the add-on options.  The schema now reads
  `ollama|ollama_cloud|moondream_cloud|moondream_local`.

### Dependency updates

- **blinkpy 0.25.7** — minor upstream fix that initialises `response_text = ""`
  before the error-logging path in `oauth_signin`, preventing a potential
  `UnboundLocalError` when the signin endpoint returns an unexpected status code.
  Our compatibility patch already avoided this code path, so the change is a
  belt-and-suspenders improvement.  `requirements.txt` and `pyproject.toml` now
  pin `blinkpy>=0.25.7`.

## 2.8.4

### Fixes

- Correct version strings across all package files so the Docker image is
  tagged and published correctly.

## 2.8.3

### New features

- **Ollama Cloud provider (`ollama_cloud`).** A fourth AI provider is now
  available that targets the [Ollama Cloud API](https://api.ollama.com).
  Set `ai_provider: ollama_cloud` and supply your API key via
  `ollama_cloud_api_key`.  The cloud provider uses the identical Ollama API
  (same `/api/generate` and `/api/tags` endpoints) so model selection,
  token counting, and vision-model auto-ranking all work exactly as with the
  local provider.

- **Ollama local over LAN.** The `ollama` provider already supported any
  URL, but this is now explicitly documented: set `ollama_url` to
  `http://<device-ip>:11434` to route analysis to an Ollama instance running
  on another device on your local network.

- **Per-clip AI analysis panel.** Each clip modal now includes a collapsible
  🤖 **AI Analysis** section.  Click to expand and see the suspicion badge,
  confidence score, AI summary, and model/timestamp.  An **Analyze Now**
  button lets you trigger analysis for any clip on demand, and a
  **Re-analyze** button re-runs it.  The full raw AI response is available
  via a **Full response** toggle.  The panel is hidden when AI is disabled.

- **Test Analysis button.** The AI Connection card on the AI tab now has a
  🔬 **Test Analysis** button.  It picks the most recently downloaded clip,
  runs a full analysis, and displays the result inline — confirming the AI
  backend is reachable and working end-to-end.

### Improvements

- Provider labels in the UI now distinguish between
  **Ollama (Local/LAN)** and **Ollama Cloud** so it is always clear which
  backend is active.
- The AI Usage tab correctly shows token counts for both `ollama` and
  `ollama_cloud` and hides the token columns for providers that do not
  report them (`moondream_cloud`, `moondream_local`).
- Startup log now reports the AI provider name and model together, making
  it easier to confirm which backend was selected.

## 2.8.2

### New features

- **AI Usage tab.** A new **📊 AI Usage** tab sits between Status and Automations
  and shows per-provider token usage statistics:
  - **Ollama** — total prompt tokens, completion tokens, and a per-model breakdown
    (extracted from the Ollama API's `prompt_eval_count` / `eval_count` fields).
  - **Moondream Cloud** — clip analysis count with a note about billing on
    moondream.ai (the Cloud API does not expose token counts).
  - **Moondream Local** — inference count only (no token tracking for on-device
    inference).

- **Token usage persisted to the database.** `tokens_prompt` and `tokens_completion`
  columns are added to the `analysis_results` table.  Existing databases are
  migrated automatically on start-up (non-destructive `ALTER TABLE` with a
  safe-ignore guard for the already-migrated case).

- **`/api/ai/usage` REST endpoint.** Returns per-model token totals and the
  current provider/model — powers the new UI tab and is available for
  external dashboards.

### Improvements

- Verified **blinkpy 0.25.6** 2FA handling: `blinkpy_compat.py` is correctly
  retained as a belt-and-suspenders patch.  `requirements.txt` pins
  `blinkpy>=0.25.6` which includes the native HTTP 202 fix (PR #1231); the
  compat patch is idempotent and harmless when running on the fixed version.

- Confirmed AI analysis disabled mode works correctly: all UI tabs render
  without errors when `ai_analysis_enabled: false`, and the AI Usage tab
  shows a helpful "no data" message until the first analysis run.

## 2.8.1

### Bug fixes

- **Fixed `moondream_local` packages not surviving container restarts.** The
  web-UI installer now installs the `moondream` package into
  `/data/moondream_packages` (a persistent HA data volume) via
  `pip install --target`.  Both the startup path (`__main__.py`) and the
  install-check helper add that directory to `sys.path` at runtime, so the
  installed model is available across restarts without re-running the
  installer.

- **Fixed `moondream_local` install hanging on aarch64 (Raspberry Pi).** The
  `moondream` package has no pre-built wheels for `aarch64` musllinux and its
  TVM dependency compilation hangs indefinitely on that platform.  The install
  endpoint now returns HTTP 422 with a clear explanation on unsupported
  architectures, and the Dockerfile skips the build-time install on non-amd64
  targets.

- **Added architecture guard in the UI.** When `moondream_local` is selected
  on a non-x86_64 host the AI tab now shows an *"unsupported architecture"*
  notice with guidance to use `moondream_cloud` or `ollama` instead, rather
  than displaying the install button.

## 2.8.0

### New features

- **Three AI analysis providers.** `ai_provider` now controls which backend
  is used to analyse camera clips:

  - `ollama` *(default, existing behaviour)* — sends frames to a
    remote/local Ollama server.  The URL and model are set via `ollama_url`
    and `ollama_model`.  The model picker in the UI now shows **only
    vision-capable models** (llava, moondream, minicpm-v, bakllava, etc.);
    text-only models are filtered out.

  - `moondream_cloud` — sends the clip's middle frame to the
    [Moondream Cloud API](https://docs.moondream.ai/api).  Requires a free
    API key (`moondream_api_key`). Handles rate-limit (HTTP 429) and invalid-
    key (HTTP 401) errors with clear log messages.

  - `moondream_local` — runs the **Moondream 0.5B INT8** model directly on
    the Raspberry Pi 5 (or any host running the add-on) using the `moondream`
    Python package.  The model file (~430 MB) is downloaded automatically on
    first use; subsequent starts reuse the cached copy.  Inference runs in a
    thread executor so the asyncio loop is never blocked.

- **AI status on the Status page.** The Status tab now shows an *AI Analysis*
  card with provider name, online/offline status, model, pending queue count,
  and suspicious-clip count — no need to navigate to the AI tab for a quick
  health overview.

- **Errors from all AI providers appear in the add-on logs** with appropriate
  log levels (`WARNING` for transient failures, `ERROR` for configuration
  problems).

### Dependency updates

- **blinkpy 0.25.6** — PR #1231 natively fixes the HTTP 202 / 2FA issue that
  our `blinkpy_compat.py` patch worked around.  The patch is kept as
  belt-and-suspenders but is now redundant.
- Added `moondream>=0.0.5` and `Pillow>=10.0.0` (required by the local
  provider).
- Dockerfile: added `jpeg-dev zlib-dev` Alpine packages (Pillow build deps).

## 2.7.2

### Bug fixes

- **Fixed clip thumbnails not appearing in the Library tab.** With
  `download_thumbnails: true`, the add-on previously only saved a thumbnail
  if Blink's `/media/changed` API returned a `thumbnail` URL for the clip —
  but that field is `null`/absent for most clips on current Blink accounts,
  so no `.jpg` was ever written and every clip fell back to the generic 🎬
  placeholder.

  Thumbnails are now generated locally with `ffmpeg` (first frame of the
  downloaded `.mp4`) immediately after each clip is saved, independent of
  what Blink's API provides. `ffmpeg` has been added to the add-on image.

- **Added a thumbnail backfill for existing libraries and reinstalls.** On
  every poll cycle, a few clips (5 by default) that are missing a `.jpg`
  thumbnail are backfilled by extracting a frame from their already-downloaded
  `.mp4`. This fills in thumbnails for clips downloaded before
  `download_thumbnails` was enabled, and — because clip files in
  `/share/blink-clips` persist across uninstall/reinstall and are re-scanned
  into the database on startup — for clips re-imported after a reinstall too.

### Tests

- `test_downloader.py` — `_generate_thumbnail` (success via real ffmpeg,
  skips when the thumbnail already exists or the video is missing, handles a
  missing `ffmpeg` binary and a non-producing ffmpeg run) and
  `backfill_thumbnails` (disabled config, no database, respects the
  per-cycle limit, only generates for clips missing a thumbnail).
- `test_app.py` — `_poll_cycle` calls `backfill_thumbnails` with the
  configured per-cycle batch size.

## 2.7.1

### Bug fixes

- **Fixed "Login failed" with no 2FA prompt, even with correct credentials
  and a valid SMS code.** After the cookie-jar fix in 2.7.0, some Blink
  accounts still got an immediate `Login failed` on the very first login
  attempt, with no 2FA prompt ever shown — yet Blink still sent an SMS
  verification code.

  The cause is upstream: blinkpy 0.25's `oauth_signin()` only recognizes an
  HTTP `412` response as "2FA required". Blink's sign-in endpoint now
  returns HTTP `202 Accepted` (with a JSON body describing the available
  SMS/voice/WhatsApp verification methods) for these accounts. blinkpy
  doesn't recognize `202`, falls through to `return None`, and
  `Auth._oauth_login_flow()` logs `Login failed` and aborts *before* raising
  `BlinkTwoFARequiredError` — so the add-on never gets to prompt for the
  code Blink just texted
  (see [fronzbot/blinkpy#1233](https://github.com/fronzbot/blinkpy/issues/1233)
  and [fronzbot/blinkpy#1230](https://github.com/fronzbot/blinkpy/issues/1230),
  both open/unfixed as of blinkpy 0.25.5).

  The add-on now patches `blinkpy.api.oauth_signin` at startup
  (`blinkpy_compat.py`) so a `202` response is treated the same as `412`,
  letting the existing 2FA prompt/submission flow run as designed. This
  workaround can be removed once a fixed blinkpy release is adopted.

### Tests

- `test_blinkpy_compat.py` — `oauth_signin` returns `"2FA_REQUIRED"` for
  HTTP `202` (and still for `412`), `"SUCCESS"` for redirect statuses, and
  `None` otherwise; the patch is applied exactly once and is idempotent.

## 2.7.0

### Bug fixes

- **Fixed "Login failed" from blinkpy's OAuth v2 flow with correct
  credentials.** blinkpy 0.25's OAuth v2 / PKCE login chains several
  requests to `api.oauth.blink.com` — an authorization request, fetching the
  sign-in page (which sets session cookies and embeds a CSRF token), then
  POSTing the username/password/CSRF token — and relies on the cookies set
  by the earlier steps being sent back on that final POST.

  The add-on created its `aiohttp.ClientSession` with aiohttp's default
  ("safe") cookie jar, which can decline to store or return some of those
  cookies. Without them, Blink's sign-in endpoint doesn't recognize the
  request as part of the same flow and returns a response that's neither a
  success redirect nor a 2FA challenge, so blinkpy logs `Login failed` /
  `Cannot setup Blink platform` and the add-on reports "Blink rejected the
  configured username/password" — even when the credentials are correct
  (see [fronzbot/blinkpy#1229](https://github.com/fronzbot/blinkpy/pull/1229)).

  `_get_session()` now creates the session with
  `aiohttp.CookieJar(unsafe=True)`, matching how a real browser/app client
  handles cookies for this flow and letting the OAuth steps share state as
  intended.

### Tests

- `test_get_session_uses_unsafe_cookie_jar` — `_get_session()` returns a
  session whose cookie jar has `unsafe=True`.

## 2.6.9

### Bug fixes

- **Fixed Blink rejecting valid credentials (and 2FA codes) on retry.**
  blinkpy's OAuth v2 / PKCE login flow identifies this installation to Blink
  with a per-device `hardware_id`, and generates a brand new random one for
  every `Auth()` instance that isn't given one. Cached tokens (including
  `hardware_id`) are only written to `/data/auth_credentials.json` after a
  *successful* login, so on a fresh install — or after any failed login —
  every retry presented Blink with a completely new, never-seen "device"
  trying to sign in with the same username/password. Repeatedly
  "registering" new devices for the same account in a short window is a
  classic credential-stuffing signal, and could cause Blink to reject
  otherwise-correct credentials outright, or invalidate a 2FA challenge
  before the code entered in the web UI could be verified (producing
  "authentication failed" even with a correct, freshly received code).

  `connect()` now generates a `hardware_id` once and persists it to
  `/data/blink_hardware_id.txt`, reusing it for every login attempt —
  including retries after a failed/expired login and across add-on restarts
  — so this installation always presents the same device identity to Blink.

- **Verified passwords containing special characters are used as configured.**
  Added regression tests that drive `connect()` with a password containing
  symbols (`p@ss!w0rd#123$%^&*()`) and confirm it reaches blinkpy's
  `login_data` unchanged.

### Tests

- `test_connect_generates_and_persists_hardware_id`,
  `test_connect_reuses_persisted_hardware_id`,
  `test_connect_adopts_hardware_id_from_auth_cache` — cover the new stable
  `hardware_id` persistence across fresh logins, retries, and cached-token
  logins.
- `test_connect_passes_through_password_with_symbols` — a password with
  symbols reaches `Auth(login_data=...)` unchanged.
- Fixed a flaky `test_falls_back_to_mtime_when_no_timestamp_in_filename` —
  on some filesystems `stat().st_mtime` can be a few milliseconds ahead of
  `datetime.now()` due to clock-source skew between the filesystem and wall
  clock; the assertion now allows a 1 s tolerance on both bounds.

## 2.6.8

### Bug fixes

- **The 2FA prompt no longer disappears without explanation.** The web UI
  polls `/api/auth/status` and opens the "Two-Factor Authentication" overlay
  while `auth_state` is `"needs_2fa"`. However, if the code wasn't submitted
  in time (the 600 s `two_fa_timeout`) — or the account setup after a
  successful 2FA submission failed — `auth_state` becomes `"error"`. The
  overlay-closing logic ran *before* the error-message block, so the overlay
  silently closed with no indication of what happened, right as the add-on
  was about to retry (and request a fresh code). From the user's
  perspective, the 2FA prompt appeared to vanish entirely.

  `checkAuthStatus()` now treats `"needs_2fa"` and `"error"` as the two
  states that keep the overlay open: on `"error"` it shows
  `auth_message` (e.g. "Verification code not provided within 600s.") in
  the dialog and re-enables the "Verify" button, instead of closing the
  overlay. The "Signed in to Blink ✓" toast and library refresh now also
  fire when recovering from `"error"` (not just `"needs_2fa"`), so a
  successful retry is reflected in the UI.

### Maintenance

- **Full operational review of the add-on.** Every module (`__main__.py`,
  `config.py`, `app.py`, `downloader.py`, `media_server.py`, `digest.py`,
  `database.py`, `storage.py`, `archiver.py`, `tracker.py`, `manifest.py`,
  `notifier.py`, `event_watcher.py`, `library_scanner.py`) plus the Docker
  and Home Assistant add-on infrastructure were reviewed end-to-end.

- **Fixed stale version metadata.** `Dockerfile`'s `ARG BUILD_VERSION`
  default (used for local/CI builds that don't pass `--build-arg
  BUILD_VERSION`) was stuck at `2.5.5` since the 2.6.0 release; it now
  matches the add-on version. `blink_downloader/__init__.py`'s
  `__version__` was also stuck at the original `1.0.0` placeholder and now
  matches.

- **Fixed incorrect repository URLs.** `DOCS.md`'s installation
  instructions pointed at the placeholder
  `github.com/yourusername/ha-blink-clip-downloader`, and the Dockerfile's
  `org.opencontainers.image.source` label was missing the `35` suffix from
  the actual GitHub username. Both now point to
  `github.com/brianbaggs35/ha-blink-clip-downloader`.

## 2.6.7

### Dependencies

- **Upgraded `blinkpy` to `>=0.25.5`** (replacing the `>=0.23.0,<0.25.0` pin
  added in 2.6.2). blinkpy 0.25 rewrote login around an OAuth v2 / PKCE flow:

  - `Blink.start()` no longer raises `UnauthorizedError` for bad
    credentials or an expired refresh token — it now returns `False`.
  - `Blink.send_2fa_code(code)` now returns `True`/`False` to indicate
    whether the code was accepted (and performs the full post-2FA account
    setup itself), instead of always returning `None` and requiring a
    second call to `start()`.

  2.6.2 worked around this by pinning blinkpy below 0.25. This release
  instead updates `downloader.py` to handle the new return-value-based
  contract directly, so the add-on can use the current blinkpy release.

### Bug fixes

- **`connect()` now treats `start()` returning `False` the same as an
  authentication failure.** Previously this only checked for
  `UnauthorizedError` (which blinkpy >= 0.25 no longer raises for bad
  credentials), so a failed login with the new blinkpy could leave
  `self._blink.account_id` unset while `auth_state` stayed
  `"authenticating"` forever. `connect()` now checks
  `started and self._blink.account_id` after `start()` returns: if either is
  falsy and cached credentials were used, it deletes the stale cache and
  retries once with the configured username/password (as before); otherwise
  it raises `AuthenticationError` with the same actionable message as before.
  The `UnauthorizedError` handler is kept (and treated the same way) in case
  a future blinkpy version raises it again.

- **2FA submission no longer calls `start()` a second time.** With blinkpy >=
  0.25, `send_2fa_code()` already completes the full login/account setup and
  reports success or failure via its return value — a wrong or expired code
  returns `False` without raising. `_submit_2fa_code()` now returns that
  value directly instead of calling `start()` again afterwards, which with
  the new OAuth flow would re-run the whole login and could trigger another,
  unexpected 2FA prompt right after a successful submission.

### Tests

- `test_connect_start_returns_false_without_cache_raises_authentication_error`,
  `test_connect_start_returns_false_with_cache_retries_fresh` — `connect()`
  handles blinkpy >= 0.25's `start()` returning `False` for both fresh and
  cached logins.
- `test_submit_2fa_code_returns_false_when_send_2fa_code_returns_false`,
  `test_handle_2fa_wrong_code_returns_false_does_not_raise` — a wrong 2FA
  code rejected via blinkpy >= 0.25's `False` return (instead of a raised
  `BlinkTwoFARequiredError`) is handled the same as before.
- Fixed a flaky `test_falls_back_to_mtime_when_no_timestamp_in_filename` —
  it captured the `before` timestamp *after* creating the clip file, so the
  file's mtime could be a few microseconds earlier than `before` and fail
  the `before <= ts <= after` assertion. `before` is now captured before the
  file is created.

## 2.6.6

### New features

- **Re-import existing clips after a reinstall** — `/data` (and the clip
  library database within it) is wiped when the add-on is uninstalled, but
  `download_path` (typically `/share/blink-clips`) is not. Previously,
  reinstalling and pointing at a folder full of existing clips left the web
  UI empty until new clips were downloaded.

  On startup, if `enable_library_db` is set, the add-on now scans
  `download_path` for `.mp4` files that aren't yet indexed and adds them to
  the library database (`blink_downloader/library_scanner.py`,
  `ClipDatabase.get_all_file_paths()`). Camera name and recording timestamp
  are recovered from the `<camera>/<YYYY-MM-DD>/<camera>_<timestamp>.mp4`
  folder/file layout used by the add-on, falling back to the filename alone
  or the file's modification time when that information isn't available.
  Files already in the database and anything under an `archives/` directory
  are skipped, so this is safe to run on every startup.

### Bug fixes

- **Fixed the 2FA dialog getting stuck after an incorrect code** —
  `Blink.send_2fa_code()` discards its result and `Blink.start()` re-raises
  `BlinkTwoFARequiredError` when the submitted code is wrong. `_handle_2fa()`
  didn't catch this, so an incorrect code propagated out as a generic
  connection error: `auth_state` stayed stuck on `"needs_2fa"` while the web
  UI's "Verify" button remained disabled with no way to retry.

  `_handle_2fa()` now submits the code through a new `_submit_2fa_code()`
  helper that catches `BlinkTwoFARequiredError` (and other submission
  errors) and reports success/failure instead of raising. On a rejected
  code, the add-on stays in `_handle_2fa()` and waits for another
  submission (until `two_fa_timeout` elapses) rather than failing the whole
  connection attempt.

  `submit_two_fa_code()` now returns a sequence number, and
  `/api/auth/status` reports `two_fa_result_seq` / `two_fa_result_ok` for
  the most recently processed submission. The web UI tracks the sequence
  number of its in-flight submission and, if it comes back rejected,
  re-enables the "Verify" button with an "Incorrect verification code"
  message and lets the user try again — instead of leaving the dialog
  stuck on "Verifying…".

### Tests

- `test_get_all_file_paths_returns_paths`, `test_get_all_file_paths_empty` —
  `ClipDatabase.get_all_file_paths()`.
- `test_library_scanner.py` — covers importing clips from the standard
  `<camera>/<date>/` layout, skipping already-known files and `archives/`
  directories, deriving camera/timestamp from filenames when not organized
  into folders, falling back to file mtime, and idempotent re-scans.
- `test_run_imports_existing_clips_with_library_db_enabled`,
  `test_run_skips_import_when_library_db_disabled` — `app.run()` re-imports
  pre-existing clips on startup only when `enable_library_db` is set.
- `test_submit_two_fa_code_returns_incrementing_seq`,
  `test_submit_2fa_code_returns_false_on_two_fa_required`,
  `test_submit_2fa_code_returns_false_on_generic_exception`,
  `test_submit_2fa_code_returns_true_on_success`,
  `test_handle_2fa_wrong_code_does_not_raise_and_keeps_waiting`,
  `test_handle_2fa_wrong_code_then_correct_code_succeeds` — wrong-code
  handling in `_handle_2fa()` / `_submit_2fa_code()`.
- `test_auth_status_forwards_two_fa_result_fields`,
  `test_two_fa_submit_returns_seq_from_callback` — `two_fa_result_seq` /
  `two_fa_result_ok` are forwarded through `/api/auth/status` and
  `/api/auth/2fa`.

## 2.6.5

### Bug fixes

- **Fixed stale cached credentials overriding an updated username/password**
  — blinkpy persists `username` and `password` (alongside the auth tokens)
  to `/data/auth_credentials.json`. On the next start, `connect()` merged
  this cached data into `login_data` *after* setting it from the add-on
  configuration, so the stale cached username/password silently won
  out over newly-configured credentials.

  This is exactly the failure seen when Blink invalidates the cached
  refresh token (e.g. after a forced password reset): the add-on would
  keep retrying with the *old* password from the cache even though the
  user had already updated the configuration, producing an endless
  `blinkpy.auth.UnauthorizedError` ("Unable to refresh token. Invalid
  refresh token or invalid credentials.") every retry.

  **`downloader.py` `connect()`** now re-applies `self._config.username`
  and `self._config.password` after merging the cached auth data, so the
  configured credentials always take precedence while cached token/host
  data is still reused.

- **Fixed unhelpful empty authentication error messages** — blinkpy's
  `UnauthorizedError` carries no message, so `str(e)` is always `""`. This
  produced log lines with nothing after the colon, e.g.:

  ```
  ERROR  blink_downloader.downloader: Authentication failed with provided credentials: 
  ERROR  blink_downloader.app: Failed to connect to Blink (attempt 1):  — retrying in 60 s
  ```

  When a fresh (non-cached) login is rejected with HTTP 401,
  `downloader.connect()` now raises a new `AuthenticationError` with an
  actionable message explaining that Blink rejected the configured
  username/password and how to fix it. `app._connect_with_retry()` catches
  `AuthenticationError` separately (like `TwoFARequired`), logs the
  descriptive message without a noisy stack trace, and sends a single HA
  persistent notification ("Blink Authentication Failed") on the first
  failed attempt instead of repeating every retry.

### Tests

- `test_connect_cached_credentials_do_not_override_config` — cached
  `username`/`password` in `auth_credentials.json` no longer override the
  add-on's configured credentials (cached token/host data is still merged).
- `test_connect_unauthorized_without_cache_raises_authentication_error` — a
  401 on a fresh login raises `AuthenticationError` with a non-empty,
  actionable message and sets `auth_state = "error"`.
- `test_connect_with_retry_notifies_on_authentication_error` —
  `_connect_with_retry()` sends one "Blink Authentication Failed"
  notification and keeps retrying after an `AuthenticationError`.

## 2.6.4

### Bug fixes

- **Fixed HTTP session reuse after failed auth** — When cached tokens fail with `UnauthorizedError`, the HTTP session was remaining in a bad state. Now we properly close the stale session before retrying with fresh credentials, ensuring a clean connection attempt.
- **Improved auth error logging** — Added distinction between "invalid cached tokens" (auto-retried) and "invalid credentials" (user error) with clearer log messages.

## 2.6.3

### Bug fixes

- **Fixed stale cached auth token handling** — When cached Blink auth tokens expire, the app now automatically deletes the cache and retries with fresh credentials instead of failing immediately.
- **Improved UnauthorizedError handling** — Added explicit catch for `UnauthorizedError` to distinguish between invalid credentials and stale tokens, with automatic retry logic for stale tokens.
- **Better error diagnostics** — Added more detailed logging to help identify whether auth failures are due to bad credentials or API issues.

## 2.6.2

### Bug fixes

- **Fixed blinkpy 0.25.5 authentication issues** — Pinned blinkpy to `>=0.23.0,<0.25.0` as v0.25.5 introduced a regression where `start()` fails silently without raising exceptions. Enhanced error detection to catch incomplete auth state and retry properly.
- **Improved 2FA flow after successful start()** — Changed 2FA handler to call `start()` again after code submission instead of just `refresh()`, ensuring full initialization of blink object.
- **Better auth error messages** — Added logging to distinguish between login failures and silent initialization failures to aid in debugging credential issues.

## 2.6.1

### Improvements

- **Updated Docker build workflow** — Migrated from deprecated `home-assistant/builder` action to modern `docker/build-push-action` with proper multi-arch manifest support.
- **Fixed YAML lint issues** — Ensured all workflow files comply with yamllint standards.

## 2.6.0

### Bug fixes

- **Fixed 2FA authentication flow** — Added `refresh()` call after 2FA submission to ensure the blink object is fully initialized with all sync modules and networks.
- **Fixed `'NoneType' object has no attribute 'base_url'` error** — Improved URL resolution with safer null checks to handle edge cases where the blink object might not be fully initialized.
- **Fixed incomplete authentication detection** — Added validation after Blink auth completes to detect incomplete initialization and force fresh login if needed.
- **Fixed cached auth handling** — Clears stale cached credentials when authentication fails or is incomplete, forcing a fresh login on retry.

## 2.5.9

### Bug fixes

- Fixed some more bugs.

## 2.5.6

### Bug fixes

- Fixed automatic library refresh while browsing clips so the page will no longer jump back to the top unexpectedly.

## 2.5.5

### New features

- **Sync Module USB local-storage download** (`download_local_storage: false`)
  — When a USB drive is plugged into a Blink Sync Module the module records
  clips to it as well as to the cloud.  Enabling this option instructs the
  add-on to also fetch those locally-stored clips each poll cycle.

  **How it works:**
  1. After every normal cloud download the add-on iterates `blink.sync` to
     find any Sync Module that reports `local_storage = True`.
  2. For each such module it calls `sync.update_local_storage_manifest()` to
     fetch the current USB clip list via the Blink cloud API (no direct LAN
     access is required).
  3. New clips (not already in the tracker) are downloaded using blinkpy's
     `LocalStorageMediaItem.prepare_download()` + `download_video()`.
  4. Downloaded clips are saved to the same `download_path` directory tree as
     cloud clips (respecting `organize_by_camera` / `organize_by_date`) and
     indexed in the SQLite clip library with `source = "local_storage"`.
  5. Clips skipped by `is_over_quota()` halt the batch for that poll cycle to
     prevent storage overruns.

  The clip IDs are prefixed with `local_` to avoid collisions with cloud
  clip IDs.  The feature is opt-in and disabled by default.

### Improvements

- **`web.FileResponse`-based video streaming** — The previous manual
  `aiofiles`-based chunked stream loop has been replaced with aiohttp's
  built-in `web.FileResponse`.  On Linux (including Raspberry Pi OS) aiohttp
  delegates the transfer to the kernel's `sendfile(2)` system call, which
  copies bytes directly from the filesystem page cache to the socket buffer
  without passing through the Python interpreter.  The result is:
  - **Lower CPU usage** — the Python GIL is not held during byte transfer;
    the asyncio event loop stays responsive for other requests.
  - **Automatic Range support** — `206 Partial Content` byte-range requests
    (used by the browser for seeking) are handled natively by aiohttp/the
    kernel, with no Python loop involved.
  - **Smoother seek performance on Raspberry Pi 5** — the Pi's kernel page
    cache warms quickly via `sendfile`, and seeks that land on cached pages
    resolve with essentially zero latency.
  - Removed the `aiofiles` and `re` dependencies from `media_server.py`.

- **Video.js `enableSmoothSeeking: true`** — When the user clicks or scrubs
  in the seek bar Video.js now plays forward at 2× speed to reach the target
  position rather than issuing an immediate jump.  The result is a fluid
  "glide" instead of a hard cut, eliminating the black-frame stall that
  previously appeared during seeks on the Pi 5.

### Tests

- `test_download_local_storage_no_blink_returns_empty` — returns `[]` when
  `_blink` is `None`.
- `test_download_local_storage_skips_no_usb` — skips sync modules whose
  `local_storage` attribute is falsy.
- `test_download_local_storage_handles_manifest_error` — logs a warning and
  continues when `update_local_storage_manifest()` raises.
- `test_download_local_storage_skips_already_tracked` — does not re-download
  clips that are already in the tracker.
- `test_download_local_storage_downloads_new_clip` — happy-path: clip is
  downloaded, written to disk, added to tracker, and added to the DB.
- `test_download_local_storage_download_failure_skipped` — when
  `download_video()` returns `False` the clip is excluded from results.
- `test_poll_cycle_calls_local_storage_when_enabled` — `_poll_cycle` calls
  `download_local_storage_clips()` when the option is enabled.
- `test_poll_cycle_skips_local_storage_when_disabled` — `_poll_cycle` does
  not call `download_local_storage_clips()` when the option is disabled.
- `test_poll_cycle_local_storage_clips_trigger_notification` — local-storage
  clips returned alongside cloud clips produce a combined HA notification.
- `test_download_local_storage_defaults_to_false` — `AppConfig` defaults to
  `download_local_storage = False`.
- `test_download_local_storage_can_be_enabled` — `_parse_config` honours
  `download_local_storage: True`.

Total: **285 tests** (up from 274 in v2.5.4); coverage 91 %.

## 2.5.4

### Bug fixes

- **Fixed Storage section showing nothing in the sidebar and Status page** —
  `_handle_stats` was reading `request.app.get("disk_stats")` which queries
  aiohttp's internal Application data store — a completely separate dict that
  is never populated with disk information.  The actual disk data lives in
  `self.extra_status` (the `MediaServer` instance dict set by `app.py`).

  **Fixes applied:**
  1. **`media_server.py` `_handle_stats`** — changed the lookup from
     `request.app.get("disk_stats")` → `self.extra_status.get("disk")`.
  2. **`app.py` connection point** — `extra_status` now includes
     `"disk": self._storage.disk_stats()` immediately after a successful Blink
     login, so storage is visible even before the first clip download.
  3. **`app.py` `_on_clips_downloaded`** — refreshes `extra_status["disk"]`
     after every successful download batch.
  4. **`app.py` `_poll_cycle`** — refreshes `extra_status["disk"]` at the end
     of every poll cycle (even when no new clips were downloaded) so the web UI
     always reflects current disk usage.

- **Fixed 2FA Verify button re-enabling after successful submission** — The
  `finally` block in `submitTwoFA()` was unconditionally re-enabling the button
  and resetting its label to "Verify", making it trivial to accidentally submit
  the same code twice (which Blink rejects).

  The `finally` block has been removed.  The button is now re-enabled (with
  "Verify" text) **only on error** (HTTP failure or network exception).  On
  success it stays disabled and shows "✓ Submitted", relying on the 3-second
  `checkAuthStatus` poll to automatically close the overlay once the add-on
  confirms sign-in.

### Improvements

- **Smoother video playback** — Three complementary changes reduce the
  micro-stalls that caused choppy playback:

  1. **Larger I/O chunks** — `_handle_stream` now reads 256 KiB at a time
     (up from 64 KiB) for both full-file and range-request streaming.  Fewer
     round-trips between the server and browser means the browser can fill its
     decode buffer faster.

  2. **`Cache-Control: public, max-age=3600`** — both streaming paths now send
     this header so the browser can cache video segments locally.  Seeking to an
     already-watched position no longer triggers a new server request.

  3. **Video.js `preload: 'auto'` and native HTML5 video** — The embedded
     player now pre-loads video content immediately instead of waiting until the
     user clicks play (`preload: 'metadata'` → `preload: 'auto'`).  The VHS
     override (`overrideNative`) has been disabled for plain MP4 progressive
     downloads so the browser's own highly-optimised video decoder handles
     buffering directly without going through the VHS (HLS) layer.

### Tests

- `test_stats_returns_disk_from_extra_status` — asserts that `/api/stats`
  returns the `disk` object when `extra_status["disk"]` is set.
- `test_stats_no_disk_when_extra_status_empty` — asserts the `disk` key is
  absent when `extra_status` is empty (server just started).
- `test_stream_full_has_cache_control` — full-file stream response includes a
  `Cache-Control` header.
- `test_stream_range_has_cache_control` — partial-content (206) response also
  includes `Cache-Control`.
- `test_poll_cycle_updates_disk_stats_in_extra_status` — `_poll_cycle` always
  refreshes `extra_status["disk"]`, even when no new clips were downloaded.
- `test_on_clips_downloaded_updates_disk_stats_in_extra_status` — download
  callback also updates `extra_status["disk"]`.

## 2.5.3

### Bug fixes

- **Fixed `int() argument must be … not 'NoneType'` — clips failing to
  download** — The Blink API returns `null` (→ Python `None`) for `duration`,
  `network_id`, and sometimes `source` on live-view clips and certain camera
  types.  `dict.get(key, default)` only uses its default when the key is
  **absent** — when the key is present with a `None` value the default is
  ignored and `None` is returned.  Calling `int(None)` then raises `TypeError`.

  **Root cause locations and fixes:**

  1. **`database.py` `add_clip`** — Changed `int(clip.get("duration", 0))`,
     `int(clip.get("network_id", 0))`, and `int(clip.get("size_bytes", 0))`
     to use `or 0` (`int(clip.get("duration") or 0)`, etc.) so a present-but-
     null value is coerced to `0` before the `int()` call.  Same treatment for
     the `str()` fields.

  2. **`downloader.py` `_download_clip` result dict** — The `result` dict
     passed to `add_clip` now normalises nullable fields at the source:
     `"duration": int(clip.get("duration") or 0)`,
     `"network_id": int(clip.get("network_id") or 0)`,
     `"source": str(clip.get("source") or "")`.
     This also protects the HA event payload and webhook call which consume
     the same dict.

  3. **`downloader.py` stale warning** — Updated the `"Clip has no address"`
     warning (leftover from before the `address`→`media` fix in 2.5.2) to
     `"Clip has no media URL"`.

- **Regression tests added**:
  - `test_add_clip_with_null_fields` in `test_database.py` — inserts a clip
    where all nullable integer fields are explicitly `None`; asserts they land
    as `0` in the database.
  - `test_download_clip_null_api_fields` in `test_downloader.py` — exercises
    `_download_clip` end-to-end with a clip whose `duration`, `network_id`, and
    `source` are all `None`; asserts the returned result dict has safe defaults.

## 2.5.2

### Bug fixes

- **Fixed "Clip has no address, skipping" — no clips downloading** — The Blink
  API returns the video URL in a field named `"media"`, not `"address"`.  Our
  `_download_clip` was calling `clip.get("address", "")` which always returned
  an empty string, causing every clip to be skipped with the "has no address"
  warning.

  **Changes in `downloader.py`:**
  - `clip.get("address", "")` → `clip.get("media", "")` (the correct Blink API
    field name, as used in blinkpy's own `_parse_downloaded_items`).
  - Added a `deleted` filter in `_apply_filters`: clips where
    `clip.get("deleted", False)` is truthy are now silently skipped before the
    download stage, matching blinkpy's own behaviour.

  **Test fixture updated** (`conftest.py`): `sample_clip` now uses `"media"`
  instead of `"address"` to reflect the real API response shape.

### Improvements

- **Web UI now follows the HA theme** — The Blink Clips panel defaults to a
  **light theme** that matches Home Assistant's default UI.  The theme
  automatically switches to dark when the operating system or browser prefers
  dark mode (`prefers-color-scheme: dark`) — the same signal HA uses for its
  own default theme.

  A **☀ / 🌙 toggle button** in the top-right of the nav bar lets users
  override the automatic choice.  The preference is stored in `localStorage`
  and persists across page loads.

  All previously hardcoded dark-mode colours (`#0d2818`, `#1a3055`, `#a9d1f7`,
  etc.) are now CSS custom properties (`--badge-ok-bg`, `--tag-bg`,
  `--code-color`, etc.) so both themes render correctly.

## 2.5.1

### Bug fixes

- **Fixed `AttributeError: 'dict' object has no attribute 'status'` — clips not
  downloading** — Our `_fetch_clip_list` was treating the return value of
  `blinkpy.api.request_videos()` as an aiohttp response object (checking
  `.status` and calling `.json()`).  In blinkpy ≥ 0.22 the library returns
  the **parsed JSON dict directly** (via `auth.query → validate_response` with
  `json_resp=True`).  Non-200 responses raise exceptions rather than returning
  an error response object.

  **Changes in `downloader.py`:**
  - Removed `response.status` and `await response.json()` calls from
    `_fetch_clip_list`.
  - Now treats the `request_videos()` return value as a dict and reads
    `data.get("media") or []` directly.
  - Wrapped the `request_videos()` call in a `try/except` so any blinkpy
    exception (`UnauthorizedError`, `BlinkBadResponse`, etc.) is caught,
    logged, and results in an empty list rather than an unhandled crash.
  - Added a `isinstance(data, dict)` guard for unexpected return types.

  **Tests updated** to mock `request_videos` returning dicts (not mock
  response objects), and the error-path test now uses `side_effect=Exception`
  to reflect how blinkpy actually signals failures.

## 2.5.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`
  — definitive fix** — Root cause was the AppArmor profile missing the `k`
  (file-lock) permission flag on `/run/`.  Without `k`, AppArmor silently
  blocks `fcntl(F_SETLK)` calls.  s6-svscan uses `fcntl`-based locking to
  acquire an exclusive lock on `/run/service/.s6-svscan/lock` at startup;
  when that call is blocked it reports "another instance already running"
  even when no competing process exists.  This was the true root cause in
  every prior version — the service-structure and ENTRYPOINT changes in
  2.2.x–2.4.0 were all red herrings.

  **Changes modelled on the official `home-assistant/apps-example` repo:**

  1. **`apparmor.txt` completely rewritten** — replaced the hand-rolled
     per-path rules with the canonical HA add-on AppArmor pattern:
     - `file,` (blanket file access — supersedes all individual `r`/`w`/`x`
       rules and implicitly includes the `k` lock flag)
     - `/run/{,**} rwk,` — explicit `rwk` so `fcntl(F_SETLK)` is permitted
       on every path under `/run/`, which is exactly what s6-svscan needs.

  2. **Switched from `s6-rc.d/` to `services.d/`** — the official example
     uses the S6-overlay v3 *legacy services* path
     (`/etc/services.d/<name>/run` + `finish`), not `s6-rc.d/`.  Removed
     `rootfs/etc/s6-overlay/s6-rc.d/` entirely; created
     `rootfs/etc/services.d/blink-downloader/run` and `finish`.

  3. **Service script shebangs corrected** — `run` uses
     `#!/usr/bin/with-contenv bashio`; `finish` uses `#!/usr/bin/env bashio`,
     matching the official example exactly.

  4. **`Dockerfile` chmod targets updated** — now points at
     `/etc/services.d/blink-downloader/{run,finish}`.

## 2.4.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`
  — for real this time** — The root cause was identified with the help of
  concrete diagnostic guidance: the previous fix (`ENTRYPOINT ["/run.sh"]`
  with `exec /init "$@"` inside `run.sh`) was itself **the bug**.

  The HA base image's `ENTRYPOINT ["/init"]` already starts s6-overlay
  exactly once.  Our wrapper called `/init` a second time (even via `exec`),
  which triggered a second s6-svscan — producing the "another instance"
  crash.  The correct pattern for HA OS add-ons is to **never override
  ENTRYPOINT and never call `/init` or `s6-svscan` from any script**.

  **Changes:**
  - Removed `ENTRYPOINT ["/run.sh"]` from `Dockerfile` — the base image's
    own `ENTRYPOINT ["/init"]` is used unchanged.
  - `rootfs/run.sh` no longer calls `/init`; it is a reference-only file
    and is not invoked by any startup mechanism.
  - The `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` script
    (the actual s6 service entry point) remains `exec python3 -m blink_downloader`
    with no s6 or init commands.

- **Fixed maintainer name not appearing in HA add-on repository** —
  `repository.yaml` at the repo root was still using the placeholder
  `Your Name <your@email.com>`.  Updated to `Brian Baggs <brianbaggs@hotmail.com>`.
  Also updated the placeholder GitHub URL in `config.yaml`, `Dockerfile`, and
  `repository.yaml` from `yourusername` to `brianbaggs`.

### Internal

- `rootfs/run.sh` updated with a clear comment explaining that the HA base
  image manages s6-overlay startup and that no script should call `/init`.

## 2.2.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`**
  — The `s6-rc.d/user/contents.d` bundle registration approach used in v2.1.x
  conflicts with the supervision tree the HA base image already owns.  Switched
  to the **`/etc/services.d/`** legacy service format, which S6-overlay v3
  supports via its backward-compatibility layer and does not touch the user
  bundle or start a second svscan.  Also added a `finish` script to the service
  directory that prevents rapid crash-restart loops (10 s back-off on unexpected
  exits).

- **Fixed "App not running — Start?" ingress loop** — The Python process was
  calling `sys.exit(1)` on configuration errors and returning immediately on
  Blink authentication failures.  Both killed the aiohttp web server, leaving
  port 8099 silent and causing HA ingress to report the add-on as not running.
  Three code-path changes fix this:

  1. **`__main__.py`** — removed `sys.exit(1)`.  On any `load_config()` error
     the process now creates a minimal `AppConfig` with `startup_error` set and
     continues into the normal app lifecycle.

  2. **`app.py` — startup-error mode** — when `startup_error` is set the web
     server starts as normal, the auth state is set to `"error"` (visible on
     the Status tab), and the process sleeps in a loop until SIGTERM rather than
     exiting.  HA ingress sees port 8099 up and the sidebar panel loads.

  3. **`app.py` — `_connect_with_retry()`** — replaces the bare `try/except`
     that returned on auth failure.  On `TwoFARequired` or any other Blink
     exception the add-on sends the HA notification, logs the error, waits
     `_reconnect_interval` seconds (default 60), and retries indefinitely.
     The process never exits between retries; the web server stays up the whole
     time.  SIGTERM is responded to promptly because the wait loop checks
     `_running` every second.

### Improvements

- Added `startup_error: str = ""` field to `AppConfig`; set by `__main__` when
  `load_config()` raises, consumed by `app.run()` to enter web-only mode.
- `_reconnect_interval` and `_startup_poll_interval` instance attributes on
  `BlinkClipDownloaderApp` (default 60 s and 1 s respectively) can be overridden
  in tests to keep the suite fast without patching `asyncio.sleep`.
- `services.d` `finish` script: logs exit code and adds a 10 s sleep before S6
  restarts the service on unexpected crashes.

## 2.1.2

### Bug fixes

- **Fixed `/init: exec: line 45: s6-overlay-suexec: Permission Denied`** —
  Root cause identified through tarball inspection of the HA base image:

  **Why this error occurs:**  `s6-overlay-suexec` is a *setuid-root* ELF
  binary whose real path inside the container is
  `/package/admin/s6-overlay-helpers-<version>/command/s6-overlay-suexec`.
  It is exposed via a two-hop symlink chain:
  `/command/s6-overlay-suexec` →
  `/package/admin/s6-overlay-helpers/command/s6-overlay-suexec` →
  real binary.  AppArmor resolves symlinks to their real path when checking
  `execve` permissions — so the `/command/** mrix,` rule that was already
  in the profile covered only the *symlink name*, not the *binary the kernel
  actually loads*.  The `Permission Denied` was AppArmor denying exec on
  `/package/admin/s6-overlay-helpers-*/command/s6-overlay-suexec`.

  **Fixes applied to `apparmor.txt`:**

  1. Added `/package/** mrix,` — covers all S6-overlay real binary paths
     (`s6-overlay-helpers`, `s6`, `s6-rc`, `s6-linux-init`, `execline`,
     `s6-portable-utils`, etc.) regardless of version number.

  2. Added Linux capabilities block:
     `capability setuid, setgid, chown, dac_override, fowner,
     net_bind_service` — `s6-overlay-suexec` is setuid root and calls
     `setresuid()`/`setresgid()` to switch UIDs; without `capability setuid`
     and `capability setgid` the kernel refuses the UID-switch even after
     the `execve` succeeds.

  3. Expanded runtime path coverage: `/run/service/** rwix,`,
     `/etc/services.d/**`, `/etc/cont-init.d/**`, `/etc/cont-finish.d/**`,
     `/etc/fix-attrs.d/**` — all paths the S6 startup sequence reads and
     executes from.

  4. Added broad `/bin/** mrix,`, `/usr/bin/** mrix,`, `/sbin/** mrix,`,
     `/lib/** mr,`, `/usr/lib/** mr,` — S6 supervision scripts invoke many
     Alpine utilities; without these the shell inside S6 service scripts
     could not execute basic commands.

  5. Removed over-specific `deny /etc/** w,` / `deny /bin/** wl,` /
     `deny /sbin/** wl,` deny rules that conflicted with the new broad
     execute rules.  The profile relies on AppArmor's default-deny posture
     for anything not explicitly allowed; the only retained `deny` is
     `deny /root/** rw,` to protect the root home directory.

## 2.1.1

### Bug fixes

- **Fixed `/bin/sh: can't open /init: Permission denied` — root cause found and
  eliminated** — Thorough research against HA OS 17.3 / Supervisor 2026.x revealed
  three compounding issues that together produced the error:

  1. **AppArmor profile was blocking S6's own init binary.**  HA Supervisor 2026.x
     enforces AppArmor more strictly than earlier versions.  The profile was missing
     explicit allow rules for `/init` (the S6-overlay ELF binary), the `/command/`
     directory (where S6v3 stores `with-contenv` and all supervision binaries),
     `/bin/sh`, `/bin/bash`, and the full S6 runtime state paths
     (`/run/s6/**`, `/run/s6-rc*/**`, `/run/service/**`,
     `/run/container_environment/**`).  Without `/init mrix,`, the kernel denied
     `open()` on the init binary and the error surfaced exactly as seen.
     All missing rules have been added to `apparmor.txt`.

  2. **Wrong `with-contenv` shebang path.**  The service `run` script used
     `#!/usr/bin/with-contenv bashio`.  The canonical, forward-compatible path in
     S6-overlay v3 HA base images is `#!/command/with-contenv bashio` — this is
     what every official HA add-on uses and what the base image's own AppArmor
     baseline expects.  Updated in both
     `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` and `rootfs/run.sh`.

  3. **Missing `dependencies.d/base` declaration.**  S6-overlay v3 requires each
     longrun service to contain an empty file at
     `s6-rc.d/<service>/dependencies.d/base` to declare that it must not start
     until the base bundle has fully initialised.  Without it the service could
     be launched before the container environment (including `SUPERVISOR_TOKEN`)
     was ready.  The file has been added.

- **Git execute bit set on service `run` script** — `git update-index
  --chmod=+x` is now applied to
  `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` and `rootfs/run.sh`
  so the execute permission is embedded in the repository (`100755`) and
  survives a clean clone without relying solely on the Dockerfile `chmod`.

## 2.1.0

### Bug fixes

- **Fixed `/bin/sh: can't open /init: Permission denied` crash on HA OS** —
  Switched from the S6-overlay v3 `CMD`-based one-shot mechanism to a proper
  S6v3 **longrun service definition** at
  `/etc/s6-overlay/s6-rc.d/blink-downloader/`.  The old `CMD ["/run.sh"]`
  approach triggered an S6v3 internal shutdown path that calls `/init` via
  `/bin/sh`; `/init` is mode 711 in the base image so the shell read fails.
  Using a named longrun service bypasses that shutdown path entirely.
- **AppArmor updated** — added `/etc/s6-overlay/**` read and
  `/run/s6*/**` read-write rules so the S6v3 runtime state directories are
  accessible within the add-on's AppArmor sandbox.
- **Fixed `webhook_url` schema** — changed from `"url?"` to `"str?"` so
  leaving the field blank no longer causes `expected a URL` validation errors
  when saving add-on configuration.
- **Fixed base image tag** — corrected `build.yaml` and `Dockerfile` to use
  the full arch-prefixed Alpine tag
  (`ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.20`), resolving
  the `image not found` build error.
- **Fixed UTC date mismatch in stats** — `get_stats()` and `get_camera_stats()`
  now use `datetime.now(timezone.utc).date()` instead of `date.today()`,
  preventing incorrect "today" counts in US timezones after 5 pm UTC−5/−8.

### Improvements

- **Web 2FA UI** — a sanitised 6-digit input overlay appears automatically in
  the Blink Clips web panel whenever Blink requires a verification code; no
  more manual `/data/two_fa_code.txt` file editing.
- **HA Blink integration coexistence** — the add-on and the built-in HA Blink
  integration can run side-by-side without conflict.  They use independent API
  sessions and separate credential storage (`/data/auth_credentials.json` vs.
  HA's own storage); the add-on does not touch any Blink-integration entities.

## 2.0.0

### New features

- **Web library UI** — built-in Video.js media server (port 8099 / HA ingress sidebar panel)
  with clip grid, thumbnails, search, camera/date/source/tag filters, sort, starred filter,
  and a camera sidebar.
- **SQLite clip library** — all downloaded clips are indexed in `/data/clip_library.db`,
  enabling fast filtering, tag management, and starred clips.
- **Video.js player** — in-browser streaming with seek, fullscreen, PiP, loop,
  theater mode, autoplay-next, and configurable playback rates.
- **Bulk ZIP export** — select multiple clips and download them as a single ZIP archive.
- **Tag support** — add/remove freeform tags per clip; filter the library by tag.
- **Keyboard shortcuts** — Space/F/M/L/Esc/↑↓/←→ with a `?` help overlay.
- **Browser notifications** — opt-in desktop notifications when new clips arrive.
- **Activity heatmap** — 7-day clip count chart on the Status tab.
- **Event-driven instant download** — subscribe to HA `state_changed` events and
  trigger a fast-poll immediately when a Blink motion sensor fires.
- **Fast-poll mode** — configurable burst polling after motion events
  (`fast_poll_interval`, `fast_poll_duration`, `post_motion_delay`).
- **Daily digest** — scheduled HA notification summarising downloads and storage.
- **ZIP archiving** — compress clips older than a threshold into monthly ZIPs.
- **Minimum clip duration filter** — skip clips shorter than N seconds.
- **HA ingress panel** — automatic sidebar entry "Blink Clips" via `ingress: true`;
  all web UI API calls use the `X-Ingress-Path` prefix so ingress and direct access
  both work correctly.
- **Retry delay** — configurable `retry_delay` (base seconds, multiplied per attempt).

### Improvements

- `config.yaml`: added ingress, panel icon/title, `retry_delay` option, corrected
  `max_storage_gb` type to `float`, removed placeholder `image` field.
- `Dockerfile`: added `io.hass.*` OCI labels and `BUILD_ARCH` / `BUILD_VERSION` ARGs
  for multi-arch HA OS builds.
- `apparmor.txt`: added `/tmp/`, `/run/s6-linux-init-container-results/exitcode`,
  and `site-packages` paths required for Python and S6-overlay operation.
- `translations/en.yaml`: added `retry_delay` translation.
- Test coverage raised to 88 % across 245 tests; event_watcher coverage 98 %.

## 1.0.0 — Initial release

- Continuous polling for new Blink camera clips
- Organise by camera name and date
- Configurable filename format with `{camera}`, `{timestamp}`, `{date}`, `{time}`, `{id}` tokens
- Storage quota management and auto-retention policy
- Camera whitelist filtering
- Motion-only clip filter
- Time-window filter (e.g. nighttime only)
- Download JPEG thumbnails alongside clips (optional)
- Configurable concurrent downloads with retry/back-off
- Home Assistant persistent notifications
- HA custom event `blink_clip_downloaded` per clip
- Virtual sensor `sensor.blink_downloader_status`
- Webhook URL support
- Newline-delimited JSON clip manifest at `/data/clip_manifest.json`
- Statistics snapshot at `/data/stats.json`
- File-based 2FA code entry via `/data/two_fa_code.txt`
- Manual trigger via `/data/trigger_download`
- Cached auth tokens for restart-free operation
- Graceful shutdown on SIGTERM / SIGINT
