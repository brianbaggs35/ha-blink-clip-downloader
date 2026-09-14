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
  <!-- Opening a clip was mouse-only: the card carried the click handler but
       nothing put it in the tab order or responded to a key. It is the
       primary control of the whole Library, so it is a real <button> now —
       native focus, native Enter/Space, and a name screen readers announce,
       none of which a focusable <div> with a button role actually gets
       right.
       That is also why the select-mode checkbox is a sibling of the button
       rather than sitting inside it, where it used to live: a control
       nested inside a button is invalid, and browsers and assistive tech
       both handle it badly. It is absolutely positioned against the card,
       whose padding box starts exactly where .thumb-wrap's did, so it lands
       in the same corner it always has. -->
  <div class="clip-card" :class="{ selected }" :data-id="clip.id">
    <button type="button" class="clip-open" :aria-label="`Open the clip from ${clip.camera}`" @click="$emit('click')">
      <div class="thumb-wrap">
        <img v-if="!thumbFailed" :src="clipThumbUrl(clip.id)" loading="lazy" alt="" @error="thumbFailed = true" />
        <div v-else class="no-thumb">
          <AppIcon name="no-thumb" />
        </div>
        <div v-if="clip.starred" class="star-badge">★</div>
        <div v-if="clip.notified" class="notified-badge">🔔</div>
        <div v-if="clip.face_recognized" class="face-badge" title="An enrolled household member was recognized">👤</div>
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
    </button>
    <input
      v-if="selectable"
      type="checkbox"
      class="sel-check"
      :checked="selected"
      aria-label="Select clip"
      @click="$emit('check')"
    />
  </div>
</template>
