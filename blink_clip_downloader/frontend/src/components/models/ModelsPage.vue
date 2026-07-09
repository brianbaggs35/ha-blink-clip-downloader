<script setup lang="ts">
interface ProviderInfo {
  key: string
  name: string
  tagline: string
  configFields: string[]
  notes: string[]
  link: { href: string; label: string }
}

const PROVIDERS: ProviderInfo[] = [
  {
    key: 'ollama',
    name: 'Ollama (Local/LAN)',
    tagline: 'Sends frames to an Ollama server running a vision model on your local network. Free and fully private — no data leaves your home.',
    configFields: ['ollama_url — URL of your Ollama server, e.g. http://192.168.1.10:11434', 'ollama_model — vision model to use (e.g. llava:7b, llama3.2-vision:11b)'],
    notes: ['Use "Fetch Models" on the AI tab to browse available models — only vision-capable models are shown, text-only models are filtered out.'],
    link: { href: 'https://ollama.com/library', label: 'Browse Ollama models' },
  },
  {
    key: 'ollama_cloud',
    name: 'Ollama Cloud',
    tagline: 'Uses the Ollama Cloud API instead of a local server. Identical API surface to local Ollama, so model selection and token counting work the same way.',
    configFields: ['ollama_url — leave empty to use the default Ollama Cloud endpoint', 'ollama_model — model name (same as local Ollama)', 'ollama_cloud_api_key — API key from api.ollama.com'],
    notes: [],
    link: { href: 'https://api.ollama.com', label: 'api.ollama.com' },
  },
  {
    key: 'moondream_cloud',
    name: 'Moondream Cloud',
    tagline: 'Sends frames to the Moondream Cloud API. Billed per request; reasoning mode is enabled by default for better spatial analysis.',
    configFields: ['moondream_api_key — API key from moondream.ai'],
    notes: ['Also supports fine-tuning — see the Fine-Tuning card on the AI tab (only shown for this provider).'],
    link: { href: 'https://docs.moondream.ai/api', label: 'Moondream Cloud API docs' },
  },
  {
    key: 'moondream_local',
    name: 'Moondream Local (0.5B)',
    tagline: 'Runs Moondream directly on the host device via the Install button on the AI tab.',
    configFields: [],
    notes: [
      'Hardware note: on-device inference requires an NVIDIA CUDA or Apple Silicon GPU — pure-CPU inference is not offered by the moondream package\'s 1.x line. Most Home Assistant OS hosts have no GPU passed through to the add-on container, so this provider reports itself unavailable on them (both amd64 and aarch64). Use moondream_cloud or ollama instead if that applies to you.',
    ],
    link: { href: 'https://moondream.ai', label: 'moondream.ai' },
  },
  {
    key: 'anthropic',
    name: 'Anthropic (Claude)',
    tagline: 'Uses the Anthropic Claude API for high-quality vision analysis. Billed per token.',
    configFields: ['anthropic_api_key — API key from console.anthropic.com', 'anthropic_model — Claude model to use, defaults to claude-haiku-4-5'],
    notes: ['Claude models receive a dedicated system prompt that separates role/format instructions from user content, improving JSON compliance. claude-haiku-4-5 is the default as the most cost-effective option.'],
    link: { href: 'https://console.anthropic.com', label: 'console.anthropic.com' },
  },
  {
    key: 'openai',
    name: 'OpenAI (GPT)',
    tagline: 'Uses the OpenAI API for vision analysis. Billed per token. Any OpenAI vision model is supported (GPT-4o, GPT-4.1, GPT-4-Turbo, GPT-5, and variants).',
    configFields: ['openai_api_key — API key from platform.openai.com', 'openai_model — GPT model to use, defaults to gpt-4o-mini'],
    notes: [
      'GPT models use response_format: json_object to guarantee valid JSON output, and "high" image detail for better scene analysis. The o1/o3/o4-mini reasoning models and the entire GPT-5 family use max_completion_tokens instead of the legacy max_tokens parameter — handled automatically based on the model name.',
    ],
    link: { href: 'https://platform.openai.com', label: 'platform.openai.com' },
  },
]
</script>

<template>
  <div class="auto-content">
    <h2>AI Providers &amp; Models</h2>
    <p>
      Six AI providers are supported for clip analysis — set <code>ai_provider</code> in the add-on's Configuration
      tab to one of the values below. Use <strong>Fetch Models</strong> on the AI tab to browse live model
      availability and pricing for the provider you've configured; this page is a reference for choosing between
      them.
    </p>

    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 1rem; margin: 1.2rem 0 1.5rem">
      <div v-for="p in PROVIDERS" :key="p.key" class="card" style="padding: 1.2rem">
        <h3 style="margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.5rem">
          {{ p.name }}
          <code style="font-size: 0.72rem; font-weight: 400; color: var(--muted)">{{ p.key }}</code>
        </h3>
        <p style="font-size: 0.85rem; color: var(--text); margin-bottom: 0.6rem; line-height: 1.5">{{ p.tagline }}</p>
        <ul v-if="p.configFields.length" style="font-size: 0.8rem; color: var(--muted); margin: 0 0 0.6rem 1.1rem; line-height: 1.6">
          <li v-for="field in p.configFields" :key="field">{{ field }}</li>
        </ul>
        <p v-for="note in p.notes" :key="note" style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem; line-height: 1.5">
          {{ note }}
        </p>
        <a :href="p.link.href" target="_blank" rel="noopener" class="btn sm ghost">↗ {{ p.link.label }}</a>
      </div>
    </div>

    <h3 style="margin-bottom: 0.75rem">🪜 Two-tier escalation (any provider)</h3>
    <p style="font-size: 0.85rem; color: var(--muted); line-height: 1.55; max-width: 70ch">
      Most motion clips are not suspicious, so paying for a strong model on every clip is wasted spend or wasted
      time. When <code>ai_escalation_provider</code> is set, every clip is first analyzed with the normal
      <code>ai_provider</code>/model (tier 1). Only clips tier 1 flags as suspicious are re-analyzed by the
      escalation provider (tier 2) for a closer second opinion, and the tier-2 verdict — not tier 1's — is what gets
      recorded and alerted on. Tier 2 can be any provider, including a different one than tier 1 — e.g. a fast
      OpenAI model escalating to Moondream Cloud or Claude for a closer second look. Track tier-1 vs. escalation
      token usage and cost separately on the <strong>AI Usage</strong> tab.
    </p>
  </div>
</template>
