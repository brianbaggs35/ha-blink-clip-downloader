<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { armCamera, armSyncModule, getSyncModules } from '../../api/syncModule'
import type { SyncModuleInfo } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import SyncModuleCard from './SyncModuleCard.vue'

// Armed state can change from the Blink app itself, another HA session, or
// a schedule/automation outside this add-on entirely — a light poll while
// this tab is actually open keeps it honest, same reasoning as
// SecurityFeedPage's own timer. Only runs while mounted (this page is
// v-if-gated in App.vue), so it costs nothing when the tab isn't open.
const POLL_INTERVAL_MS = 30_000

const toast = useToastStore()
const refresh = useRefreshStore()
const confirm = useConfirm()
const DISARM_CONFIRM_MESSAGE = 'No camera will record on motion until you arm it again.'

const loading = ref(true)
const loadError = ref(false)
const syncModules = ref<SyncModuleInfo[]>([])
const armingAll = ref(false)
// Captured at the moment the hero button is clicked so the in-flight label
// ("Arming…"/"Disarming…") reflects the action actually underway, rather
// than re-deriving it from systemArmed -- which would happen to also work
// today (state doesn't change until the reload after the request settles)
// but would silently go stale if that ever changed.
const armingAllTarget = ref(false)
const pendingModules = ref<Set<string>>(new Set())
const pendingCameras = ref<Set<string>>(new Set())

let loadSeq = 0
let pollTimer: ReturnType<typeof setInterval> | undefined

async function load() {
  const seq = ++loadSeq
  loading.value = true
  loadError.value = false
  try {
    const result = await getSyncModules()
    if (seq !== loadSeq) return
    syncModules.value = result
  } catch {
    if (seq === loadSeq) loadError.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

async function silentReload() {
  // Swap in place rather than the full loading-spinner flow above — same
  // "don't blank the page for a background refresh" reasoning as
  // SecurityFeedPage/ArchivedClipsSection's own refresh.tick handling.
  const seq = ++loadSeq
  try {
    const result = await getSyncModules()
    if (seq === loadSeq) {
      syncModules.value = result
      // Also clears the initial spinner if this call wins the race against
      // onMounted's own load() (e.g. a refresh.tick from another tab lands
      // before the very first fetch does) -- load()'s own seq check would
      // then correctly skip clearing it, since real data already arrived
      // here first, and it must not stay stuck on the spinner forever
      // waiting for a load() call that's no longer the most recent.
      loading.value = false
    }
  } catch {
    // Transient — keep showing whatever's already on screen.
  }
}

// "Armed" requires every sync module itself armed *and* every one of their
// cameras individually armed (motion detection on) -- disarming even a
// single camera while its module stays armed is a real, meaningful
// difference from the camera-app's point of view (that one camera won't
// record on motion), so the headline status must reflect it rather than
// only ever tracking the module-level flag.
const systemArmed = computed(
  () => syncModules.value.length > 0 && syncModules.value.every((m) => m.armed && m.cameras.every((c) => c.armed)),
)
// A disarmed sync module makes every one of its cameras stop recording
// regardless of their own motion-detection flag, so "fully disarmed" is
// still purely module-level -- camera state is irrelevant here.
//
// m.armed === false specifically (not just falsy): blinkpy's own `arm`
// property returns null, not false, whenever network_info hasn't been
// populated/parsed yet (e.g. right after a reconnect) -- a bare `!m.armed`
// would count that unknown state as "confirmed disarmed" and claim the
// system is unprotected when the real state simply isn't known yet.
// systemStatusLabel's "Partially Armed" fallback (with its warning-
// triangle icon) is what a module in that state falls into instead, which
// correctly signals "needs a look" rather than a false "all clear".
const systemDisarmed = computed(() => syncModules.value.length > 0 && syncModules.value.every((m) => m.armed === false))
const systemStatusLabel = computed(() => {
  if (systemArmed.value) return 'System Armed'
  if (systemDisarmed.value) return 'Disarmed'
  return 'Partially Armed'
})
// A distinct PrimeIcon per state (not just a color change) so the status
// reads at a glance even without color — shield (protected), open lock
// (nothing protected), warning triangle (needs a look, not fully either).
const systemStatusIcon = computed(() => {
  if (systemArmed.value) return 'pi pi-shield'
  if (systemDisarmed.value) return 'pi pi-lock-open'
  return 'pi pi-exclamation-triangle'
})
const cameraCount = computed(() => syncModules.value.reduce((n, m) => n + m.cameras.length, 0))
// A disarmed sync module stops every one of its cameras from recording
// regardless of their own motion-detection flag (same fact
// systemDisarmed's own comment above relies on) -- so a camera only counts
// toward this readout while its parent module is armed too, or the
// subtitle could contradict the headline right next to it (e.g. "Disarmed"
// next to "2 of 2 cameras armed" for a module disarmed via the hero button
// without touching either camera's own toggle).
const armedCameraCount = computed(() =>
  syncModules.value.reduce((n, m) => n + (m.armed ? m.cameras.filter((c) => c.armed).length : 0), 0),
)
const systemHeroButtonLabel = computed(() => {
  if (armingAll.value) return armingAllTarget.value ? 'Arming…' : 'Disarming…'
  return systemArmed.value ? 'Disarm Entire System' : 'Arm Entire System'
})

// A module's own switch counts as "disarming the entire system" too once
// it's the last one still armed (the common single-sync-module account
// makes this true on every disarm) — the confirmation must catch that
// path as well, or it'd be a one-click loophole around the hero button's
// own confirmation below.
function wouldFullyDisarmSystem(moduleName: string): boolean {
  return syncModules.value.filter((m) => m.name !== moduleName).every((m) => !m.armed)
}

async function toggleSystemArmed() {
  // Guards against a double-click firing a second overlapping run — the
  // button is also :disabled/:loading while armingAll is true, but this
  // is a second, independent line of defense at the handler itself.
  if (armingAll.value) return
  const target = !systemArmed.value
  if (!target) {
    if (!(await confirm(DISARM_CONFIRM_MESSAGE, 'Disarm the entire system?'))) return
  }
  armingAll.value = true
  armingAllTarget.value = target
  try {
    const results = await Promise.all(
      syncModules.value.map(async (m) => {
        try {
          await armSyncModule(m.name, target)
          return { name: m.name, ok: true }
        } catch {
          return { name: m.name, ok: false }
        }
      }),
    )
    await silentReload()
    const failed = results.filter((r) => !r.ok)
    if (failed.length) {
      toast.show(`Could not ${target ? 'arm' : 'disarm'}: ${failed.map((f) => f.name).join(', ')}`, true)
    } else {
      toast.show(`Entire system ${target ? 'armed' : 'disarmed'}`)
    }
  } finally {
    armingAll.value = false
  }
}

async function onModuleToggle(name: string, armed: boolean) {
  // Same double-click guard as toggleSystemArmed — the switch itself is
  // also :disabled while pending, this is the belt-and-suspenders check.
  if (pendingModules.value.has(name)) return
  // Defensive only — every caller of onModuleToggle passes a name straight
  // from the v-for's own `mod.name` (keyed on that same value), so a
  // background reload that drops this name from syncModules also unmounts
  // the very card that could have called this with it, before a real click
  // could ever originate from it. Verified empirically (a captured
  // component reference from before such a reload really is inert
  // afterward, not just stale data) rather than assumed — genuinely
  // unreachable through the real component boundary, not merely untested,
  // so this is excluded from coverage rather than exercised through an
  // artificial direct call that wouldn't reflect how this is ever actually
  // invoked.
  const module = syncModules.value.find((m) => m.name === name)
  /* v8 ignore next */
  /* istanbul ignore next -- see v8-ignore comment above; same dead branch */
  if (!module) return
  if (!armed && wouldFullyDisarmSystem(name)) {
    if (!(await confirm(DISARM_CONFIRM_MESSAGE, 'Disarm the entire system?'))) return
  }
  pendingModules.value = new Set(pendingModules.value).add(name)
  try {
    await armSyncModule(name, armed)
    module.armed = armed
    toast.show(`${name} ${armed ? 'armed' : 'disarmed'}`)
  } catch {
    toast.show(`Failed to ${armed ? 'arm' : 'disarm'} ${name}`, true)
  } finally {
    const next = new Set(pendingModules.value)
    next.delete(name)
    pendingModules.value = next
  }
}

async function onCameraToggle(cameraName: string, armed: boolean) {
  // Same double-click guard as toggleSystemArmed/onModuleToggle.
  if (pendingCameras.value.has(cameraName)) return
  pendingCameras.value = new Set(pendingCameras.value).add(cameraName)
  try {
    await armCamera(cameraName, armed)
    for (const module of syncModules.value) {
      const cam = module.cameras.find((c) => c.name === cameraName)
      if (cam) cam.armed = armed
    }
    toast.show(`${cameraName} ${armed ? 'armed' : 'disarmed'}`)
  } catch {
    toast.show(`Failed to ${armed ? 'arm' : 'disarm'} ${cameraName}`, true)
  } finally {
    const next = new Set(pendingCameras.value)
    next.delete(cameraName)
    pendingCameras.value = next
  }
}

function stopPolling() {
  clearInterval(pollTimer)
  pollTimer = undefined
}
function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => void silentReload(), POLL_INTERVAL_MS)
}

onMounted(async () => {
  await load()
  startPolling()
})
onUnmounted(stopPolling)
watch(
  () => refresh.tick,
  () => void silentReload(),
)
</script>

<template>
  <div class="syncmodule-page">
    <h2>Sync Module</h2>
    <p class="page-intro">
      Arm or disarm your entire Blink system in one click, or fine-tune motion detection per camera — the same
      arm/disarm state as the Blink app itself.
    </p>

    <div v-if="loading" style="padding: 1rem"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" size="small" :closable="false">
      Failed to load sync modules. Try refreshing the page.
    </Message>
    <Message v-else-if="!syncModules.length" severity="info" size="small" :closable="false">
      No sync modules found — make sure Blink is connected and your account has at least one sync module.
    </Message>
    <template v-else>
      <div
        class="system-hero"
        :class="{
          'system-hero-armed': systemArmed,
          'system-hero-disarmed': systemDisarmed,
          'system-hero-mixed': !systemArmed && !systemDisarmed,
        }"
      >
        <div class="system-hero-status">
          <span class="system-hero-icon-badge"><i :class="systemStatusIcon" class="system-hero-icon" /></span>
          <div>
            <div class="system-hero-title">{{ systemStatusLabel }}</div>
            <div class="system-hero-subtitle">
              {{ armedCameraCount }} of {{ cameraCount }} camera{{ cameraCount === 1 ? '' : 's' }} armed
            </div>
          </div>
        </div>
        <Button
          class="system-hero-btn"
          size="large"
          :label="systemHeroButtonLabel"
          :severity="systemArmed ? 'danger' : 'success'"
          :loading="armingAll"
          :disabled="armingAll"
          @click="toggleSystemArmed"
        />
      </div>

      <div class="sync-module-list">
        <SyncModuleCard
          v-for="mod in syncModules"
          :key="mod.name"
          :module="mod"
          :pending="pendingModules.has(mod.name)"
          :pending-cameras="pendingCameras"
          @toggle-module="(armed) => onModuleToggle(mod.name, armed)"
          @toggle-camera="onCameraToggle"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.syncmodule-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.system-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 1.25rem;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 1.5rem;
  margin-bottom: 1.5rem;
  border-left: 6px solid var(--border);
}

.system-hero-armed {
  border-left-color: var(--success);
}

.system-hero-disarmed {
  border-left-color: var(--danger);
}

.system-hero-mixed {
  border-left-color: var(--warn);
}

.system-hero-status {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.system-hero-icon-badge {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 3.5rem;
  height: 3.5rem;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--border);
}

.system-hero-icon {
  font-size: 1.6rem;
}

.system-hero-armed .system-hero-icon-badge {
  background: color-mix(in srgb, var(--success) 18%, transparent);
}
.system-hero-armed .system-hero-icon {
  color: var(--success);
}

.system-hero-disarmed .system-hero-icon-badge {
  background: color-mix(in srgb, var(--danger) 18%, transparent);
}
.system-hero-disarmed .system-hero-icon {
  color: var(--danger);
}

.system-hero-mixed .system-hero-icon-badge {
  background: color-mix(in srgb, var(--warn) 18%, transparent);
}
.system-hero-mixed .system-hero-icon {
  color: var(--warn);
}

.system-hero-title {
  font-size: 1.4rem;
  font-weight: 700;
}

.system-hero-subtitle {
  color: var(--muted);
  font-size: 0.9rem;
}

/* The "one big click" control - deliberately larger than every per-item
   toggle below it (min-width + bigger label via PrimeVue's size="large"). */
.system-hero-btn {
  min-width: 14rem;
  font-size: 1.05rem;
  padding-top: 0.85rem;
  padding-bottom: 0.85rem;
}

.sync-module-list {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

@media (max-width: 600px) {
  .syncmodule-page {
    padding: 1rem;
  }
  .system-hero {
    flex-direction: column;
    align-items: stretch;
    padding: 1.25rem;
  }
  .system-hero-btn {
    width: 100%;
  }
}
</style>
