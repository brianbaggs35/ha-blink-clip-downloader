<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { getSecurityFeedSnapshot } from '../../api/securityFeed'

const props = defineProps<{
  camera: string
  /** Changes whenever the page wants every tile to look for a newer snapshot. */
  tick: number
}>()

// An object URL for the last snapshot that arrived whole. It is only ever
// replaced by another complete one, so a refresh that fails (a throttled
// Blink call, a dropped connection, an empty body) leaves the tile showing
// what it had instead of going black until the next tick happens to work.
const shownUrl = ref('')
// No picture yet and the last attempt failed: the tile has nothing to show.
const unavailable = ref(false)
// A picture is showing but the last refresh failed, so it may be out of date.
const stale = ref(false)
let inFlight: AbortController | undefined

async function refreshSnapshot() {
  // One request per tile at a time: a slow camera finishes what it started
  // rather than being restarted on every tick and never arriving.
  if (inFlight) return
  const controller = new AbortController()
  inFlight = controller
  try {
    const blob = await getSecurityFeedSnapshot(props.camera, controller.signal)
    if (controller.signal.aborted) return
    const previous = shownUrl.value
    shownUrl.value = URL.createObjectURL(blob)
    if (previous) URL.revokeObjectURL(previous)
    unavailable.value = false
    stale.value = false
  } catch {
    if (shownUrl.value) stale.value = true
    else unavailable.value = true
  } finally {
    inFlight = undefined
  }
}

onMounted(() => void refreshSnapshot())
watch(
  () => props.tick,
  () => void refreshSnapshot(),
)
onUnmounted(() => {
  inFlight?.abort()
  if (shownUrl.value) URL.revokeObjectURL(shownUrl.value)
})
</script>

<template>
  <div class="secfeed-tile">
    <div class="secfeed-tile-image-wrap">
      <img
        :src="shownUrl || undefined"
        :alt="`${camera} snapshot`"
        class="secfeed-tile-image"
        :class="{ 'secfeed-tile-image-hidden': !shownUrl }"
      />
      <div v-if="unavailable" class="secfeed-tile-placeholder muted-note">No snapshot available yet</div>
      <div v-if="stale" class="secfeed-tile-stale" role="status">Couldn't refresh</div>
    </div>
    <div class="secfeed-tile-name">{{ camera }}</div>
  </div>
</template>

<style scoped>
.secfeed-tile {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.secfeed-tile-image-wrap {
  position: relative;
  aspect-ratio: 16 / 9;
  background: #000;
  overflow: hidden;
}

.secfeed-tile-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.secfeed-tile-image-hidden {
  display: none;
}

.secfeed-tile-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 0.5rem;
}

.secfeed-tile-stale {
  /* Over the picture, so it reads the same in both themes. */
  position: absolute;
  right: 0.5rem;
  bottom: 0.5rem;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.7);
  color: #fff;
  font-size: 0.75rem;
}

.secfeed-tile-name {
  padding: 0.5rem 0.75rem;
  font-weight: 600;
  font-size: 0.85rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
