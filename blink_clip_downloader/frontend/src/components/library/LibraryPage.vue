<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  deleteClip,
  exportZip,
  getCameras,
  getStats,
  getTags,
  listClips,
  starClip,
  type ClipFilters,
} from '../../api/clips'
import { getAiStatus } from '../../api/ai'
import type { CameraStat, ClipListItem, LibraryStats } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useConnectionStore } from '../../stores/connection'
import { useDateFilterStore } from '../../stores/dateFilter'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import AppIcon from '../icons/AppIcon.vue'
import BulkBar from './BulkBar.vue'
import CameraNav from './CameraNav.vue'
import ClipCard from './ClipCard.vue'
import ClipModal from './ClipModal.vue'

const PAGE_SIZE = 48

const toast = useToastStore()
const confirm = useConfirm()
const connection = useConnectionStore()
const refresh = useRefreshStore()
const dateFilter = useDateFilterStore()
const clipViewer = useClipViewerStore()

// Filters
const search = ref('')
const dateRange = ref('week')
const sourceFilter = ref('')
const tagFilter = ref('')
const sortOrder = ref<'newest' | 'oldest' | 'camera' | 'size' | 'duration'>('newest')
const starredOnly = ref(false)
const notifiedOnly = ref(false)
const currentCamera = ref('all')

const tags = ref<string[]>([])
const tagsLoaded = ref(false)
const cameras = ref<CameraStat[]>([])
const stats = ref<LibraryStats | null>(null)
const clips = ref<ClipListItem[]>([])
const currentPage = ref(0)
const hasMore = ref(false)
const loadingInitial = ref(false)

const selectMode = ref(false)
const selectedIds = ref<Set<string>>(new Set())
const zipping = ref(false)

const activeClipId = ref<string | null>(null)
const aiEnabled = ref(false)
const promptDebugEnabled = ref(false)

const lastTotalCount = ref(0)

const diskPct = computed(() => {
  const disk = stats.value?.disk
  if (!disk?.quota_bytes) return null
  return Math.min(100, (disk.used_bytes / disk.quota_bytes) * 100)
})
const diskClass = computed(() => {
  const pct = diskPct.value
  if (pct == null) return ''
  return pct > 90 ? 'danger' : pct > 70 ? 'warn' : ''
})
const libSizeGb = computed(() => ((stats.value?.total_size_bytes ?? 0) / 1_073_741_824).toFixed(2))

function sinceDate(range: string): string | null {
  const d = new Date()
  if (range === 'today') d.setHours(0, 0, 0, 0)
  else if (range === 'yesterday') {
    d.setDate(d.getDate() - 1)
    d.setHours(0, 0, 0, 0)
  } else if (range === 'week') d.setDate(d.getDate() - 7)
  else if (range === 'month') d.setDate(d.getDate() - 30)
  else return null
  return d.toISOString()
}

function buildFilters(page: number): ClipFilters {
  const filters: ClipFilters = { limit: PAGE_SIZE, offset: page * PAGE_SIZE, sort: sortOrder.value }
  if (currentCamera.value !== 'all') filters.camera = currentCamera.value
  if (search.value.trim()) filters.search = search.value.trim()
  const since = sinceDate(dateRange.value)
  if (since) filters.since = since
  if (starredOnly.value) filters.starred = true
  if (notifiedOnly.value) filters.notified = true
  if (sourceFilter.value) filters.source = sourceFilter.value
  if (tagFilter.value) filters.tag = tagFilter.value
  return filters
}

async function loadClips(page: number) {
  if (page === 0) {
    loadingInitial.value = true
    clips.value = []
  }
  try {
    const result = await listClips(buildFilters(page))
    clips.value = page === 0 ? result : clips.value.concat(result)
    hasMore.value = result.length === PAGE_SIZE
    currentPage.value = page
  } catch {
    toast.show('Failed to load clips', true)
  } finally {
    loadingInitial.value = false
  }
}

async function loadClipsForDate(date: string) {
  currentPage.value = 0
  try {
    const result = await listClips({
      since: `${date}T00:00:00Z`,
      until: `${date}T23:59:59Z`,
      limit: PAGE_SIZE,
      offset: 0,
      sort: sortOrder.value,
    })
    clips.value = result
    hasMore.value = result.length === PAGE_SIZE
  } catch {
    toast.show('Failed to load clips', true)
  }
}

async function loadCameras() {
  try {
    cameras.value = await getCameras()
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.warn-only handling */
  }
}

function checkNewClipsNotification(total: number) {
  const notifEnabled = localStorage.getItem('blink_notif') === '1'
  if (
    lastTotalCount.value > 0 &&
    total > lastTotalCount.value &&
    notifEnabled &&
    'Notification' in window &&
    Notification.permission === 'granted'
  ) {
    const n = total - lastTotalCount.value
    new Notification(`🎥 ${n} new Blink clip${n > 1 ? 's' : ''}`, {
      body: 'New clips are available in your library.',
      tag: 'blink-new-clips',
    })
  }
  lastTotalCount.value = total
}

async function loadStats() {
  try {
    const s = await getStats()
    stats.value = s
    if (typeof s.connected === 'boolean') connection.setConnected(s.connected)
    checkNewClipsNotification(s.total_count || 0)
  } catch {
    /* non-fatal */
  }
}

async function loadTags() {
  if (tagsLoaded.value) return
  try {
    tags.value = await getTags()
    tagsLoaded.value = true
  } catch {
    /* non-fatal */
  }
}

async function loadAiStatus() {
  try {
    const s = await getAiStatus()
    aiEnabled.value = s.enabled
    promptDebugEnabled.value = s.prompt_debug_enabled
  } catch {
    /* non-fatal */
  }
}

async function loadAll() {
  await Promise.all([loadStats(), loadCameras(), loadClips(0), loadAiStatus()])
}

let debounceTimer: ReturnType<typeof setTimeout>
watch([search, dateRange, sourceFilter, tagFilter, sortOrder, starredOnly, notifiedOnly], () => {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => loadClips(0), 380)
})
watch(currentCamera, () => loadClips(0))
watch(() => refresh.tick, loadAll)
watch(
  () => dateFilter.seq,
  () => {
    if (dateFilter.date) void loadClipsForDate(dateFilter.date)
  },
)
watch(
  () => clipViewer.seq,
  () => {
    if (clipViewer.clipId) openModal(clipViewer.clipId)
  },
)

function onCardClick(clip: ClipListItem) {
  if (selectMode.value) toggleSelect(clip.id)
  else openModal(clip.id)
}

function toggleSelectMode(on: boolean) {
  selectMode.value = on
  selectedIds.value = new Set()
}
function toggleSelect(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

async function bulkStar() {
  if (!selectedIds.value.size) return
  const ids = [...selectedIds.value]
  await Promise.all(ids.map((id) => starClip(id, true)))
  toast.show(`Starred ${ids.length} clip(s)`)
  toggleSelectMode(false)
  void loadClips(0)
}
async function bulkDelete() {
  if (!selectedIds.value.size) return
  if (!(await confirm(`Delete ${selectedIds.value.size} clip(s) permanently?`))) return
  const ids = [...selectedIds.value]
  await Promise.all(ids.map((id) => deleteClip(id).catch(() => {})))
  toast.show(`Deleted ${ids.length} clip(s)`)
  toggleSelectMode(false)
  void loadClips(0)
  void loadStats()
}
async function bulkZip() {
  if (!selectedIds.value.size) return
  zipping.value = true
  try {
    const blob = await exportZip([...selectedIds.value])
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'blink-clips.zip'
    a.click()
    URL.revokeObjectURL(url)
    toast.show(`Downloaded ${selectedIds.value.size} clip(s) as ZIP`)
  } catch {
    toast.show('ZIP export failed', true)
  } finally {
    zipping.value = false
  }
}

function openModal(id: string) {
  activeClipId.value = id
}
function closeModal() {
  activeClipId.value = null
}
function onNav(dir: number) {
  if (!activeClipId.value) return
  const idx = clips.value.findIndex((c) => c.id === activeClipId.value)
  const next = idx + dir
  if (next >= 0 && next < clips.value.length) activeClipId.value = clips.value[next].id
}
async function onDeleted(id: string) {
  const idx = clips.value.findIndex((c) => c.id === id)
  try {
    await deleteClip(id)
  } catch {
    toast.show('Failed to delete clip', true)
    return
  }
  toast.show('Clip deleted')
  if (idx !== -1) clips.value = clips.value.filter((c) => c.id !== id)
  if (idx >= 0 && idx < clips.value.length) activeClipId.value = clips.value[idx].id
  else if (idx - 1 >= 0) activeClipId.value = clips.value[idx - 1]?.id ?? null
  else closeModal()
}
function onStarred(id: string, starred: boolean) {
  const clip = clips.value.find((c) => c.id === id)
  if (clip) clip.starred = starred
}

let pollTimer: ReturnType<typeof setInterval>
onMounted(() => {
  void loadAll()
  void loadTags()
  // Auto-refresh every 60s when the modal is closed, mirroring the pre-Vue
  // UI's interval — only stats/cameras, never the grid, so browsing isn't
  // interrupted mid-scroll.
  pollTimer = setInterval(() => {
    if (!activeClipId.value) {
      void loadStats()
      void loadCameras()
    }
  }, 60_000)
})
onUnmounted(() => {
  clearInterval(pollTimer)
  clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="lib-filters">
    <input id="search" v-model="search" class="search" type="search" placeholder="🔍 Search…" />
    <select id="date-range" v-model="dateRange" class="sel">
      <option value="">All time</option>
      <option value="today">Today</option>
      <option value="yesterday">Yesterday</option>
      <option value="week">This week</option>
      <option value="month">This month</option>
    </select>
    <select id="source-filter" v-model="sourceFilter" class="sel">
      <option value="">All sources</option>
      <option value="pir">Motion (PIR)</option>
      <option value="liveview">Liveview</option>
      <option value="snapshot">Snapshot</option>
    </select>
    <select id="tag-filter" v-model="tagFilter" class="sel">
      <option value="">All tags</option>
      <option v-for="t in tags" :key="t" :value="t">#{{ t }}</option>
    </select>
    <select id="sort-order" v-model="sortOrder" class="sel">
      <option value="newest">⬆ Newest</option>
      <option value="oldest">⬇ Oldest</option>
      <option value="camera">📷 Camera</option>
      <option value="size">💾 Size</option>
      <option value="duration">⏱ Duration</option>
    </select>
    <label class="chk"><input v-model="starredOnly" type="checkbox" /> ★ Starred</label>
    <label class="chk"><input v-model="notifiedOnly" type="checkbox" /> 🔔 Notified</label>
    <button class="btn ghost sm" @click="toggleSelectMode(!selectMode)">
      {{ selectMode ? '☒ Selecting' : '☐ Select' }}
    </button>
  </div>

  <BulkBar
    v-if="selectMode"
    :count="selectedIds.size"
    :zipping="zipping"
    @star="bulkStar"
    @delete="bulkDelete"
    @zip="bulkZip"
    @cancel="toggleSelectMode(false)"
  />

  <div class="lib-body">
    <aside class="sidebar">
      <div class="sb-head">Cameras</div>
      <CameraNav v-model="currentCamera" :cameras="cameras" />
      <div class="sb-head" style="margin-top: 0.8rem">Storage</div>
      <div id="storage-info" style="padding: 0.4rem 1rem; font-size: 0.77rem; color: var(--muted)">
        <template v-if="stats?.disk">
          <div>Used: {{ stats.disk.used_mb }} MB</div>
          <div style="color: var(--text)">Free: {{ stats.disk.free_gb }} GB</div>
          <div v-if="stats.disk.quota_bytes" class="prog-bar" style="margin-top: 0.35rem">
            <div class="prog-fill" :class="diskClass" :style="{ width: `${(diskPct || 0).toFixed(1)}%` }"></div>
          </div>
        </template>
      </div>
    </aside>
    <main class="lib-main">
      <div id="stats-bar" class="stats-row">
        <div class="stat-chip">Today <strong>{{ stats?.today_count ?? 0 }}</strong></div>
        <div class="stat-chip">Week <strong>{{ stats?.week_count ?? 0 }}</strong></div>
        <div class="stat-chip">Total <strong>{{ stats?.total_count ?? 0 }}</strong></div>
        <div class="stat-chip">★ Starred <strong>{{ stats?.starred_count ?? 0 }}</strong></div>
        <div class="stat-chip">Library <strong>{{ libSizeGb }} GB</strong></div>
      </div>
      <div id="clip-grid" class="clip-grid">
        <div v-if="loadingInitial" style="grid-column: 1 / -1; padding: 2rem; text-align: center; color: var(--muted)">
          Loading…
        </div>
        <div v-else-if="!clips.length" class="empty">
          <AppIcon name="empty-box" />
          <h3>No clips found</h3>
          <p>Try adjusting filters or tap Sync to fetch new clips.</p>
        </div>
        <template v-else>
          <ClipCard
            v-for="c in clips"
            :key="c.id"
            :clip="c"
            :selected="selectedIds.has(c.id)"
            @click="onCardClick(c)"
          />
        </template>
      </div>
      <div class="load-more-row">
        <button v-if="hasMore" class="btn outline" @click="loadClips(currentPage + 1)">Load more…</button>
      </div>
    </main>
  </div>

  <!-- Teleported to <body>: this component renders inside #page-library,
       which is display:none while another tab is active (see .page/.page.active
       in base.css) — the modal must stay visible when opened from elsewhere,
       e.g. the AI tab's suspicious-activity feed via clipViewer.requestOpen(). -->
  <Teleport to="body">
    <ClipModal
      :clip-id="activeClipId"
      :ai-enabled="aiEnabled"
      :prompt-debug-enabled="promptDebugEnabled"
      @close="closeModal"
      @nav="onNav"
      @deleted="onDeleted"
      @starred="onStarred"
    />
  </Teleport>
</template>
