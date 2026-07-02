# Changelog

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
