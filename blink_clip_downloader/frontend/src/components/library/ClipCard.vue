<script setup lang="ts">
import { ref } from 'vue'
import Tag from 'primevue/tag'
import { clipThumbUrl } from '../../api/clips'
import { fmtDur, fmtSize, fmtTs } from '../../api/constants'
import type { ClipListItem } from '../../api/types'
import AppIcon from '../icons/AppIcon.vue'

withDefaults(defineProps<{ clip: ClipListItem; selected: boolean; selectable?: boolean }>(), {
  selectable: true,
})
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
      <div v-if="clip.starred" class="star-badge">★</div>
      <div v-if="clip.notified" class="notified-badge">🔔</div>
      <div v-if="clip.face_recognized" class="face-badge" title="An enrolled household member was recognized">👤</div>
      <input
        v-if="selectable"
        type="checkbox"
        class="sel-check"
        :checked="selected"
        aria-label="Select clip"
        @click.stop="$emit('check')"
      />
    </div>
    <div class="clip-info">
      <div class="clip-camera">{{ clip.camera }}</div>
      <div class="clip-time">{{ fmtTs(clip.timestamp) }}</div>
      <div class="clip-meta">
        <Tag v-if="clip.source" severity="secondary" :value="clip.source" class="src-pill" />
        <span v-if="clip.duration">⏱ {{ fmtDur(clip.duration) }}</span>
        <span>{{ fmtSize(clip.size_bytes) }}</span>
        <Tag v-for="tag in clip.tags" :key="tag" :value="tag" class="tag-pill" />
      </div>
    </div>
  </div>
</template>
