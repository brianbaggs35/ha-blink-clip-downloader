<script setup lang="ts">
import { computed } from 'vue'
import Avatar from 'primevue/avatar'
import Button from 'primevue/button'
import { fmtNum, fmtTs } from '../../api/constants'
import type { AssistantMessage } from '../../api/types'

const props = defineProps<{ message: AssistantMessage }>()
const emit = defineEmits<{ 'open-clip': [clipId: string] }>()

const isAssistant = computed(() => props.message.role === 'assistant')
const tokens = computed(() => (props.message.prompt_tokens || 0) + (props.message.completion_tokens || 0))
</script>

<template>
  <div class="bubble-row" :class="message.role" :data-role="message.role" data-testid="assistant-message">
    <Avatar v-if="isAssistant" icon="pi pi-shield" shape="circle" class="bubble-avatar" aria-hidden="true" />
    <div class="bubble">
      <p class="bubble-text">{{ message.content }}</p>
      <div v-if="message.clips?.length" class="bubble-clips" data-testid="assistant-clips">
        <span class="bubble-label">Clips mentioned</span>
        <Button
          v-for="clip in message.clips"
          :key="clip.ref"
          size="small"
          :severity="clip.is_suspicious ? 'danger' : 'secondary'"
          outlined
          icon="pi pi-video"
          :label="`${clip.camera} · ${fmtTs(clip.timestamp)}`"
          :aria-label="`Open clip from ${clip.camera}, ${fmtTs(clip.timestamp)}`"
          data-testid="assistant-clip-chip"
          @click="emit('open-clip', clip.id)"
        />
      </div>
      <div v-if="isAssistant && (message.model || tokens)" class="bubble-foot">
        <span v-if="message.model">{{ message.model }}</span>
        <span v-if="tokens">{{ fmtNum(tokens) }} tokens</span>
        <span v-if="message.days">last {{ message.days }} day{{ message.days === 1 ? '' : 's' }} of activity</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  gap: 0.6rem;
  align-items: flex-start;
  min-width: 0;
}
.bubble-row.user {
  justify-content: flex-end;
}
.bubble-avatar {
  flex: none;
  background: var(--tag-bg);
  color: var(--tag-text);
}
.bubble {
  max-width: min(46rem, 100%);
  min-width: 0;
  padding: 0.7rem 0.95rem;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--card);
  box-shadow: var(--shadow-sm);
}
.user .bubble {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--btn-text);
  border-bottom-right-radius: 4px;
}
.assistant .bubble {
  border-top-left-radius: 4px;
}
.bubble-text {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
.bubble-clips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
  margin-top: 0.7rem;
}
.bubble-label {
  font-size: 0.75rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.bubble-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 0.9rem;
  margin-top: 0.55rem;
  font-size: 0.75rem;
  color: var(--muted);
}
</style>
