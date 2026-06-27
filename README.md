# Home Assistant Blink Clip Downloader

A Home Assistant OS add-on that continuously downloads Blink camera clips to your
local hard drive using [blinkpy](https://github.com/fronzbot/blinkpy).

## Add-ons

### [Blink Clip Downloader](blink_clip_downloader/DOCS.md)

Periodically polls the Blink API for new clips and saves them to `/share/blink-clips`
(or a path you configure). Supports per-camera organisation, retention policies, storage
quotas, Home Assistant notifications, and much more.

## Installation

1. In Home Assistant go to **Settings → Apps → Install App**.
2. Click the three-dot menu (⋮) → **Repositories**.
3. Paste `https://github.com/brianbaggs35/ha-blink-clip-downloader` and click **Add**.
4. Search for **Blink Clip Downloader** and click **Install**.
5. Fill in your Blink credentials and click **Save**, then **Start**.

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

## Support

Open an issue at <https://github.com/brianbaggs35/ha-blink-clip-downloader/issues>.

## Feature Requests

Have an idea for a new feature? Please create an issue at
<https://github.com/brianbaggs35/ha-blink-clip-downloader/issues> and use the
**Feature Request** label so it can be tracked and prioritised.
