Backend Coverage - [![codecov](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader/graph/badge.svg?token=66T4D63JFM&flag=backend)](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader?flags%5B0%5D=backend)

Frontend Coverage - [![codecov](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader/graph/badge.svg?token=66T4D63JFM&flag=frontend)](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader?flags%5B0%5D=frontend)

Combined Coverage - [![codecov](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader/graph/badge.svg?token=66T4D63JFM)](https://codecov.io/github/brianbaggs35/ha-blink-clip-downloader)

Sonarqube Scan - [![Quality gate status](https://sonarcloud.io/api/project_badges/measure?project=brianbaggs35_ha-blink-clip-downloader&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=brianbaggs35_ha-blink-clip-downloader)

CI - [![CI/CD Pipeline](https://github.com/brianbaggs35/ha-blink-clip-downloader/actions/workflows/ci.yaml/badge.svg)](https://github.com/brianbaggs35/ha-blink-clip-downloader/actions/workflows/ci.yaml)

CD - [![CI/CD Pipeline](https://github.com/brianbaggs35/ha-blink-clip-downloader/actions/workflows/build.yaml/badge.svg?event=release)](https://github.com/brianbaggs35/ha-blink-clip-downloader/actions/workflows/build.yaml)

Current Version - ![GitHub release](https://img.shields.io/github/v/release/brianbaggs35/ha-blink-clip-downloader.svg)

Add app repo to home assistant with one click (Note: You must have your home assistant URL set in settings):

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrianbaggs35%2Fha-blink-clip-downloader)

<div align="center">

## ❤️ Support This Project

If this project helps you, consider supporting its development.

<a href="https://github.com/sponsors/brianbaggs35">
<img src="https://img.shields.io/badge/GitHub%20Sponsors-Support%20Me-ea4aaa?logo=github" />
</a>

<a href="https://buymeacoffee.com/brianbaggs">
<img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Support%20Me-ffdd00?logo=buymeacoffee" />
</a>

</div>

# For Users Without Home Assistant OS

I have created a standalone platform for any users who don't use home assistant but still want the same capabilities for their
blink cameras. The repo can be found here - https://github.com/brianbaggs35/blink_downloader but please note that the app that
can be found with that link is not nearly as actively developed as this home assistant one and the home assistant one has more
features.

It uses the same blinkpy package and python/vue/typescript and also uses sonarqube and all other linters and tools to keep
code quality high. If you have any issues, create an issue for it and anyone is welcome to contribute as well just open a
pull request for review.

# Home Assistant Blink Clip Downloader

A Home Assistant OS add-on that continuously downloads Blink camera clips to your
local hard drive using [blinkpy](https://github.com/fronzbot/blinkpy).

# Note About Installation On aarch64

This image takes anywhere from 5 to 20 minutes to be built depending on your device speed. Please be patient when installing updates and rest assured it will give you an error if it fails. Before the progress just went from 0% to 100% but now it looks like that might be
fixed.

## Information and Documentation

### [Blink Clip Downloader](blink_clip_downloader/DOCS.md)

Periodically polls the Blink API for new clips and saves them to `/share/blink-clips`
(or a path you configure). Supports per-camera organisation, retention policies, storage
quotas, Home Assistant notifications, per-camera battery monitoring with low-battery
alerts, and much more.

A clip that fails to download is asked for again until it arrives, so a network blip, a
busy burst of motion or a full `max_clips_per_poll` never silently drops footage. Clips waiting on a retry, and the rare clip that is still failing after six hours
and is given up on, are shown on the web UI's Status tab.

## System Requirements

Runs on any **Home Assistant OS or Supervised** install (not Core-only —
this add-on needs the Supervisor). Supports **amd64** and **aarch64**,
verified via a real build-and-boot check against both architectures.

|                    | Minimum                                   | Recommended |
|--------------------|--------------------------------------------|-------------|
| **CPU**            | 2 cores                                     | 4 cores (Raspberry Pi 5 or better) |
| **RAM**             | 2 GB free                                   | 4 GB+ free — 8 GB total on the host (e.g. Pi 5 8GB) if you'll also enable the optional computer-vision pipeline below |
| **Disk (add-on)**   | ~4.2 GB for the Docker image alone          | Add 100 MB–800 MB+ if you enable the optional computer-vision pipeline (object detection, depth estimation, face recognition, audio analysis — downloaded once, on first use), plus ~430 MB if you use the local Moondream AI provider |
| **Disk (clips)**    | Governed entirely by your own `retention_days`/`max_storage_gb` settings | A USB SSD or NVMe HAT rather than a microSD card — this add-on's continuous polling and clip downloads generate meaningful sustained write load |
| **Architecture**   | amd64 or aarch64                            | — |

The baseline (Blink polling, clip download, web library, single-tier AI
analysis) is comfortable on the minimums above, on **any** aarch64 board
including a Raspberry Pi 4. The optional **Enhanced Detection & Tracking**
pipeline (YOLO object detection, Depth Anything V2, SAM2 contact
segmentation) is what actually needs the recommended tier — it's off by
default and analysis works identically without it. In order to use all of
the advanced features, I would recommend a raspberry pi 5 8gb or similar
device, or a mini-PC with at least an intel N100 and 8gb of ram, if you
want to run all of the advanced features. It might run on a less powerful
device, but it hasn't been tested as far as I know.

> ℹ️ **Raspberry Pi 4 or older:** the Enhanced Detection & Tracking pipeline
> depends on PyTorch, which has long-standing, still-unresolved crashes on
> the Pi 4's Cortex-A72 CPU (missing ARM instructions PyTorch's official
> builds assume are present). This add-on detects that at startup and
> automatically disables just the affected stages — a hardware-level crash
> isn't something Python can catch after the fact, so the add-on checks
> *before* ever attempting the risky import, rather than relying on you to
> know not to enable it. Everything else (clip downloading, the web
> library, single-tier AI analysis) is unaffected. Raspberry Pi 5's newer
> CPU doesn't have this limitation.

See the [full documentation](blink_clip_downloader/DOCS.md#disk-space) for
a detailed disk-space breakdown.

## Installation

1. In Home Assistant go to **Settings → Apps → Install App**.
2. Click the three-dot menu (⋮) → **Repositories**.
3. Paste `https://github.com/brianbaggs35/ha-blink-clip-downloader` and click **Add**.
4. Search for **Blink Clip Downloader** and click **Install**.
5. Fill in your Blink credentials and click **Save**, then **Start**.

### Updating from 4.0.2 or earlier

Expect this specific update to take roughly **10-20 minutes**, well beyond
the usual under-a-minute add-on restart — the Supervisor is pulling a
genuinely new ~4.2 GB image from scratch, not an incremental diff, since
4.0.2 and earlier ran on Alpine and this release switched to Debian (no
shared image layers between the two) and also added a bundled PostgreSQL
server plus the always-installed computer-vision pipeline dependencies.
This is a one-time cost of crossing that version boundary; updates after
that are back to normal. See the [CHANGELOG](blink_clip_downloader/CHANGELOG.md)
for details.

## AI Provider Setup

The add-on can analyse each downloaded clip using an AI vision model to detect
suspicious activity. Enable this by setting `ai_analysis_enabled: true` and choosing
a provider below.

### Ollama (Local LAN)

Runs a vision model on a machine inside your network — no cloud fees.

1. Install [Ollama](https://ollama.com) on a PC or server on your LAN.
2. Pull a vision-capable model, e.g. `ollama pull llama3.2-vision`.
3. In the add-on settings set:
   - `ai_provider: ollama`
   - `ollama_url: http://<your-ollama-host>:11434`
   - `ollama_model: llama3.2-vision` (or use **Fetch Models** in the web UI to pick one)

### Ollama Cloud

Uses the hosted [Ollama Cloud API](https://ollama.com/cloud) — no local GPU required.

1. Sign up at <https://ollama.com/cloud> and generate an API key.
2. In the add-on settings set:
   - `ai_provider: ollama_cloud`
   - `ollama_cloud_api_key: <your-api-key>`
   - `ollama_model: llama3.2-vision` (or another vision model available on the cloud)

You can get some free usage but you will hit your limit fairly quickly if you have an
active camera.

### Moondream Cloud

Uses the [Moondream Cloud API](https://moondream.ai) — a lightweight, low-cost
cloud vision model.

1. Sign up at <https://moondream.ai> and generate an API key.
2. In the add-on settings set:
   - `ai_provider: moondream_cloud`
   - `moondream_api_key: <your-api-key>`

No model selection is needed; the cloud always uses the latest Moondream model.

### Moondream Local

Downloads and runs the Moondream 0.5B INT8 model (~430 MB) directly on the device
running Home Assistant. No API key or internet connection is required after the
first download.

1. In the add-on settings set:
   - `ai_provider: moondream_local`

The model is downloaded automatically on first use and cached for subsequent starts.
Performance depends on your hardware; a CPU-only host will be slower than one with
a GPU.

### Anthropic Claude

Uses the [Anthropic Claude API](https://console.anthropic.com) — a highly capable
cloud vision model. Requires an Anthropic account and API key; usage is billed per
token.

1. Sign up at <https://console.anthropic.com> and create an API key.
2. In the add-on settings set:
   - `ai_provider: anthropic`
   - `anthropic_api_key: <your-api-key>`
   - `anthropic_model: claude-haiku-4-5` (most cost-effective; use **Fetch Models**
     in the web UI to see all available models)

**Cost tip:** `claude-haiku-4-5` ($1/$5 per 1M tokens) is the most affordable option
and works well for security-camera analysis. `claude-opus-4-8` ($5/$25 per 1M tokens)
gives the best accuracy for complex scenes.

### OpenAI

Uses the [OpenAI Chat Completions API](https://platform.openai.com) — supports GPT-4o,
GPT-4.1, and other vision-capable models. Requires an OpenAI account and API key;
usage is billed per token.

1. Sign up at <https://platform.openai.com> and create an API key.
2. In the add-on settings set:
   - `ai_provider: openai`
   - `openai_api_key: <your-api-key>`
   - `openai_model: gpt-4o-mini` (most cost-effective; use **Fetch Models**
     in the web UI to see all available vision models)

**Cost tip:** `gpt-4o-mini` ($0.15/$0.60 per 1M tokens) is the most affordable option
and performs well for security-camera analysis. `gpt-4o` ($2.50/$10 per 1M tokens)
offers higher accuracy, while `gpt-4.1-nano` ($0.10/$0.40 per 1M tokens) is the
lowest-cost option available.

## Protecting your car

With AI analysis on, the **Vehicles** tab is where you tell the add-on which
vehicle to protect: describe it, tick the cameras that can see it, and draw a
zone around where it parks. The AI model is then instructed to flag a person
touching, reaching into or lingering at that vehicle, or an animal jumping up
on it, and to leave people who simply walk past alone. The zone matters even
with one car in view, since it is how the add-on knows which car is yours.

Turn on **Enhanced Detection & Tracking** (`ai_enhanced_detection_enabled`,
off by default) and code measures the same things too. Object detection and
tracking, depth estimation and contact segmentation work out who was at the
car and whether they touched it. A touch those stages confirm raises an alert
even when the AI model called the clip unremarkable. Without depth and
segmentation, for example on a low-powered device, an outline overlapping the
car in the image can't be told apart from someone walking past in front of
it, so that call is left to the AI model. Face recognition on the
**Biometrics** tab can clear the routine clips of household members you
enroll, such as getting in or out of their own car. It never clears a
possible impact, a clip with someone it doesn't recognize, or evidence
strong enough to raise an alert on its own.

Details are in the docs:
[Vehicles tab](blink_clip_downloader/DOCS.md#vehicles-tab--protected-vehicle-monitoring),
[Structured Security Analysis](blink_clip_downloader/DOCS.md#structured-security-analysis),
and
[running it on a low-powered device](blink_clip_downloader/DOCS.md#running-it-on-a-low-powered-device).

## Audio Analysis (optional, off by default)

Blink cameras record sound, and the frames an AI provider sees are silent. Turn on
**Enable Audio Analysis** in the add-on's Configuration tab and the add-on also
classifies what a clip *sounds* like — breaking glass, a raised voice, a car alarm, a
door, footsteps, a power tool, a dog — and passes that to the AI as one more piece of
evidence to reconcile against the frames. Clips from a camera with no microphone, or
with it switched off, are skipped. What it heard shows up as chips under
**What was heard** in the Library clip modal's AI panel.

**It classifies sound and never transcribes speech.** There is no speech-to-text model
anywhere in this path, no transcript is produced or stored, and the sound labels are the
only thing that reaches the prompt. Classification runs locally on CPU, so the audio
itself is never uploaded — not to Hugging Face, and not to whichever AI provider you
configured, which receives the labels as text. A raised voice outside at 3am is security
evidence; a readable transcript of your neighbours' conversation is surveillance, and
this add-on will not produce one.

Breaking glass, gunfire and alarms go further than a hint: they become real
security events, scored and shown on the Security Events tab, and glass or gunfire
alone is enough to flag a clip **even when nothing at all was visible** — which is
the point, since a camera cannot see round a corner or in the dark. Everyday sounds
(speech, footsteps, a dog, a door, a power tool) stay hints and raise nothing.

Nothing is uploaded, so it costs almost nothing. The frames a clip already sends run
350-425 tokens each; the audio line is 96 tokens of text, and absent entirely when
nothing was heard. It never delays a verdict either — the model loads in the
background on first use, and clips analyzed while it downloads are simply analyzed
without an audio hint rather than waiting for it. A camera with audio recording
switched off in the Blink app is detected and skipped without troubling the model.
Full details in [the docs](blink_clip_downloader/DOCS.md#audio-analysis-and-privacy).

## Alerts you can act on from your phone

A suspicious-activity alert on the Home Assistant companion app, by email or on
Discord shows the clip's key moment (the frame where the person is, not the empty
scene before they walked in) and says when the clip was recorded in your own time
zone. Tapping it opens that clip in the add-on's panel, through Home Assistant, so it
works away from home without opening any port. On the phone, **Not a threat**
dismisses a false alarm and teaches that camera the same way the Library's
thumbs-down does. Home Assistant's own persistent notifications stay text only.
Turn the pictures off with `alert_include_image`. Details are in
[the docs](blink_clip_downloader/DOCS.md#what-a-suspicious-activity-alert-carries).

## Cached prompts/tokens

Only OpenAI and Anthropic support this. This is enabled by default and helps with the
cost.

## OpenAI free daily tokens

If you want to get access to 2.5 million free tokens per day for certain models and
you don't mind sharing your data to allow OpenAI to train their models with your data,
check out this wiki page:

https://github.com/brianbaggs35/ha-blink-clip-downloader/wiki/Free-AI-Analysis-using-OpenAI-Shared

## Model Testing Status

The following providers/models have been tested and are working or were fixed:

| Version         | Working (Tested)   |
| ----------------| -------------------|
| GPT-4o-mini     | :white_check_mark: |
| GPT-5 models    | :white_check_mark: |
| Moondream cloud | :white_check_mark: |
| Anthropic models| :x:                |
| Ollama models   | :x:                |
| Moondream local | :x:                |

## Google Drive Backup Setup

The add-on can optionally back up clips to Google Drive as an extra copy,
alongside local storage. It's fully optional and, unlike the options above,
configured entirely from the **Storage** tab in the web UI rather than
`config.yaml`.

Google requires every application to register its own OAuth client, so
there's a one-time setup step in [Google Cloud
Console](https://console.cloud.google.com/):

1. Create a project (or reuse one), enable the **Google Drive API**, and
   configure the OAuth consent screen (External is fine for personal use).
2. Create an **OAuth client ID** of type **"TVs and Limited Input devices"**
   — this add-on signs in via Google's device-flow (a short code you enter
   at `google.com/device`), since it runs behind Home Assistant ingress with
   no fixed redirect URL for a standard sign-in button.
3. Copy the **Client ID** and **Client Secret** into the Storage tab's
   **Google Drive Setup** card, click **Save Setup**, then **Connect Google
   Drive** and follow the on-screen code.

See [Storage Tab — Google Drive
Backup](blink_clip_downloader/DOCS.md#storage-tab--google-drive-backup) for
the full walkthrough — including moving the OAuth consent screen to
production (recommended; otherwise Google expires the connection weekly),
choosing a backup policy, and exactly what does and doesn't get uploaded.

## Testing and Code Quality

I have setup an extremely thorough CI pipeline that should catch most problems before
they get merged. The CI pipeline includes:

- ESlint
- Pyright
- Home Assistant Addon Linter
- YAMLlint
- Ruff
- Bandit security scans
- Prettier
- Vue/Typescript type checks
- Pytest (100% backend coverage)
- Vitest (100% frontend coverage)
- Playwright e2e (92% of all lines covered as of Sep. 18th, 2026 but always adding more)
- Sonarqube scans
- Codecov coverage reports
- Built addon images and trivy scans
- Smoke tests on the addon containers built and scanned with trivy
- Full home assistant supervisor container that installs and runs the app and then
verifies it

If any one of those checks fail, the entire PR fails. I use claude code to help with the
development so I also built a very thorough CI pipeline to detect any issues or mistakes
made by the AI.

## Support

Open an issue at <https://github.com/brianbaggs35/ha-blink-clip-downloader/issues>.

## Feature Requests

Have an idea for a new feature? Please create an issue at
<https://github.com/brianbaggs35/ha-blink-clip-downloader/issues> and use the
**Feature Request** label so it can be tracked and prioritised.

## Note

I pay for my Claude subscription out of pocket and that helps me develop and fix bugs quicker. If you want to
help contribute, click the buy me a coffee link. Thank you!
