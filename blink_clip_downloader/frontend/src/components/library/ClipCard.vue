<script setup lang="ts">
import { ref } from 'vue'
import { clipThumbUrl } from '../../api/clips'
import { fmtDur, fmtSize, fmtTs } from '../../api/constants'
import type { ClipListItem } from '../../api/types'
import AppIcon from '../icons/AppIcon.vue'

defineProps<{ clip: ClipListItem; selected: boolean }>()
defineEmits<{ click: []; check: [] }>()

const thumbFailed = ref(false)
</script>

<template>
  <div class="clip-card" :class="{ selected }" :data-id="clip.id" @click="$emit('click')">
    <div class="thumb-wrap">
      <img v-if="!thumbFailed" :src="clipThumbUrl(clip.id)" loading="lazy" alt="" @error="thumbFailed = true" />
      <div v-else class="no-thumb">
        <AppIcon name="no-thumb" />
      </div>
      <div v-if="clip.duration" class="dur-badge">{{ fmtDur(clip.duration) }}</div>
      <div v-if="clip.starred" class="star-badge">★</div>
      <div v-if="clip.notified" class="notified-badge">🔔</div>
      <div class="sel-check" role="checkbox" :aria-checked="selected" @click.stop="$emit('check')">
        {{ selected ? '✓' : '' }}
      </div>
    </div>
    <div class="clip-info">
      <div class="clip-camera">{{ clip.camera }}</div>
      <div class="clip-time">{{ fmtTs(clip.timestamp) }}</div>
      <div class="clip-meta">
        <span v-if="clip.source" class="src-pill">{{ clip.source }}</span>
        <span v-if="clip.duration">⏱ {{ fmtDur(clip.duration) }}</span>
        <span>{{ fmtSize(clip.size_bytes) }}</span>
        <span v-for="tag in clip.tags" :key="tag" class="tag-pill">{{ tag }}</span>
      </div>
    </div>
  </div>
</template>
