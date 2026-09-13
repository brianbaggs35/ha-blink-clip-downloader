<script setup lang="ts">
import AppIcon from '../icons/AppIcon.vue'
import type { IconName } from '../icons/paths'

// Shared icon + heading + hint-text layout for "nothing here yet" states,
// promoted from Library's own .empty (base.css) — one of at least three
// near-identical, differently-named bespoke versions of the same pattern
// found across the app (AiAnalysisConfigCard's .empty-state, UsagePage's
// inline-styled paragraph). Still renders the same .empty class so the
// existing base.css styling (and Library's own look) is unchanged.
withDefaults(defineProps<{ icon?: IconName; title: string }>(), { icon: 'empty-box' })
</script>

<template>
  <div class="empty">
    <AppIcon :name="icon" />
    <h3>{{ title }}</h3>
    <p><slot /></p>
  </div>
</template>
