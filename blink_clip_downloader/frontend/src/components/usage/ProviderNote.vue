<script setup lang="ts">
// Renders the same per-provider explanatory copy as the pre-Vue UI's
// _PROVIDER_NOTES/_ESCALATION_NOTE constants, but as real template markup
// instead of v-html'd HTML strings — this codebase structurally disallows
// v-html (see eslint.config.js) so this bug class can't reappear even if a
// provider note is ever built from something less trusted than a hand-typed
// constant.
defineProps<{ provider?: string; showEscalationNote: boolean }>()
</script>

<template>
  <div style="font-size: 0.8rem; color: var(--muted); margin-top: 0.6rem; line-height: 1.55">
    <p v-if="provider === 'ollama'">
      Ollama (Local/LAN) runs on your own hardware or another device on your network — no cloud costs. Token counts are
      extracted from the Ollama API response (<code>prompt_eval_count</code> / <code>eval_count</code>). Some cached
      responses may show 0 prompt tokens.
    </p>
    <p v-else-if="provider === 'ollama_cloud'">
      Ollama Cloud (api.ollama.com) is a hosted Ollama service. Token counts are extracted from the API response. API
      usage may incur costs — check your Ollama Cloud account dashboard.
    </p>
    <p v-else-if="provider === 'moondream_cloud'">
      Moondream Cloud bills per API request. Each frame is analysed individually with reasoning mode enabled for better
      spatial accuracy. Token counts shown are <em>estimates</em> (256 image tokens + text tokens per frame) — the
      Moondream API does not return usage stats. Also supports fine-tuning — see the Fine-Tuning card above. Check
      <a href="https://moondream.ai" target="_blank" rel="noopener">moondream.ai</a> for authoritative billing.
    </p>
    <p v-else-if="provider === 'moondream_local'">
      Moondream Local runs entirely on-device — no cloud costs and no token tracking. The analysis count shows how many
      clips have been processed.
    </p>
    <p v-else-if="provider === 'anthropic'">
      Anthropic (Claude) charges per token. Input and output tokens are tracked for every analysis. Use
      <strong>Claude Haiku 4.5</strong> for best cost efficiency ($1/$5 per 1M tokens). Estimated cost is calculated
      from your token usage and the model's current pricing.
    </p>
    <p v-else-if="provider === 'openai'">
      OpenAI charges per token. Input and output tokens are tracked from the API response for every analysis.
    </p>
    <p v-if="showEscalationNote">
      Two-tier escalation (<code>ai_escalation_provider</code> / <code>ai_escalation_model</code>) works with any
      provider as tier 2, including a different one than tier 1 — e.g. a fast OpenAI model escalating to Moondream Cloud
      or Claude for a closer second look. When configured, tier-1 and escalation tokens/cost are tracked and priced
      separately (see the escalation row in the table below).
    </p>
  </div>
</template>
