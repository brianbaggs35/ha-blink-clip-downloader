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
