import type { AiProvider } from './types'

// Single source of truth for the provider→label map that used to be
// duplicated verbatim in three places (Status, AI, Usage tabs) in the
// pre-Vue `_HTML` script.
export const PROVIDER_LABELS: Record<AiProvider, string> = {
  ollama: 'Ollama (Local/LAN)',
  ollama_cloud: 'Ollama Cloud',
  moondream_cloud: 'Moondream Cloud',
  moondream_local: 'Moondream Local (0.5B)',
  anthropic: 'Anthropic (Claude)',
  openai: 'OpenAI (GPT)',
}

export function providerLabel(provider?: string): string {
  if (!provider) return '—'
  return PROVIDER_LABELS[provider as AiProvider] ?? provider
}

// Provider explanatory copy shown on the AI Usage tab — preserved verbatim
// (including the moondream.ai link) from the pre-Vue `_HTML` script's
// `_PROVIDER_NOTES`. Rendered via `v-html` in a single trusted, hand-authored
// spot — never built from user input.
export const PROVIDER_NOTES: Partial<Record<AiProvider, string>> = {
  ollama:
    'Ollama (Local/LAN) runs on your own hardware or another device on your network — no cloud costs. Token counts are extracted from the Ollama API response (<code>prompt_eval_count</code> / <code>eval_count</code>). Some cached responses may show 0 prompt tokens.',
  ollama_cloud:
    'Ollama Cloud (api.ollama.com) is a hosted Ollama service. Token counts are extracted from the API response. API usage may incur costs — check your Ollama Cloud account dashboard.',
  moondream_cloud:
    'Moondream Cloud bills per API request. Each frame is analysed individually with reasoning mode enabled for better spatial accuracy. Token counts shown are <em>estimates</em> (256 image tokens + text tokens per frame) — the Moondream API does not return usage stats. Also supports fine-tuning — see the Fine-Tuning card above. Check <a href="https://moondream.ai" target="_blank" rel="noopener">moondream.ai</a> for authoritative billing.',
  moondream_local:
    'Moondream Local runs entirely on-device — no cloud costs and no token tracking. The analysis count shows how many clips have been processed.',
  anthropic:
    'Anthropic (Claude) charges per token. Input and output tokens are tracked for every analysis. Use <strong>Claude Haiku 4.5</strong> for best cost efficiency ($1/$5 per 1M tokens). Estimated cost is calculated from your token usage and the model\'s current pricing.',
  openai:
    'OpenAI charges per token. Input and output tokens are tracked from the API response for every analysis.',
}

export const ESCALATION_NOTE =
  'Two-tier escalation (<code>ai_escalation_provider</code> / <code>ai_escalation_model</code>) works with any provider as tier 2, including a different one than tier 1 — e.g. a fast OpenAI model escalating to Moondream Cloud or Claude for a closer second look. When configured, tier-1 and escalation tokens/cost are tracked and priced separately (see the escalation row in the table below).'

export function fmtNum(n: number | null | undefined): string {
  if (n == null || n === 0) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

export function fmtCost(cost: number | null | undefined): string {
  if (cost == null) return 'N/A'
  return cost < 0.001 ? '<$0.001' : '$' + cost.toFixed(4)
}

export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

export function fmtRelative(ts: string | null | undefined): string {
  if (!ts) return ''
  const d = (Date.now() - new Date(ts).getTime()) / 1000
  if (d < 60) return 'just now'
  if (d < 3600) return Math.floor(d / 60) + 'm ago'
  if (d < 86400) return Math.floor(d / 3600) + 'h ago'
  return Math.floor(d / 86400) + 'd ago'
}

export function fmtSize(bytes: number | null | undefined): string {
  if (!bytes) return ''
  if (bytes >= 1_073_741_824) return (bytes / 1_073_741_824).toFixed(2) + ' GB'
  if (bytes >= 1_048_576) return (bytes / 1_048_576).toFixed(1) + ' MB'
  return (bytes / 1024).toFixed(0) + ' KB'
}

export function fmtDur(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const m = Math.floor(seconds / 60)
  const sec = Math.round(seconds % 60)
  return m ? `${m}m ${sec}s` : `${sec}s`
}
