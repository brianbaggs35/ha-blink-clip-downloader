<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Paginator from 'primevue/paginator'
import Panel from 'primevue/panel'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { fmtSize, fmtTs } from '../../api/constants'
import { deleteClip, getCameras, listClips } from '../../api/clips'
import { getArchiveGroups, runArchiveNow } from '../../api/storage'
import type { ArchiveGroup, ClipListItem } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// Archives (one row per shared monthly ZIP) rather than individual clips --
// a library with thousands of archived clips still only has a few dozen
// distinct ZIPs, so this list is small enough to paginate with real page
// numbers (see pagedGroups below) and to hand the camera/date filters
// without needing a dedicated total-count endpoint.
const GROUPS_PER_PAGE = 10

const toast = useToastStore()
const confirm = useConfirm()

const loading = ref(true)
const loadError = ref(false)
const allGroups = ref<ArchiveGroup[]>([])
const cameras = ref<string[]>([])
const cameraFilter = ref('all')
const sinceFilter = ref('')
const untilFilter = ref('')
const first = ref(0)

// Per-archive clip cache: fetched lazily the first time a panel is
// expanded, not up front -- opening one archive shouldn't require loading
// every clip across every archive.
const clipsByArchive = ref<Record<string, ClipListItem[]>>({})
const loadingArchive = ref<Record<string, boolean>>({})
const archiveLoadError = ref<Record<string, boolean>>({})
const expandedArchives = ref<Set<string>>(new Set())
const deletingId = ref<string | null>(null)
const archivingNow = ref(false)

async function loadGroups() {
  loading.value = true
  loadError.value = false
  // A changed camera filter invalidates any already-fetched per-archive
  // clip lists (they'd show every camera again, contradicting the
  // freshly-filtered summary counts) -- clear them and collapse everything
  // rather than let a stale, differently-filtered list linger if the panel
  // happened to already be open.
  clipsByArchive.value = {}
  expandedArchives.value = new Set()
  try {
    allGroups.value = await getArchiveGroups({
      camera: cameraFilter.value === 'all' ? undefined : cameraFilter.value,
      since: sinceFilter.value || undefined,
      until: untilFilter.value ? `${untilFilter.value}T23:59:59` : undefined,
    })
    first.value = 0
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

async function loadCameras() {
  try {
    cameras.value = (await getCameras()).map((c) => c.camera).sort((a, b) => a.localeCompare(b))
  } catch {
    // Non-fatal -- the camera filter is just empty (besides "All") if this fails.
  }
}

onMounted(() => {
  void loadCameras()
  void loadGroups()
})
defineExpose({ reload: loadGroups })

watch([cameraFilter, sinceFilter, untilFilter], () => void loadGroups())

function clearFilters() {
  cameraFilter.value = 'all'
  sinceFilter.value = ''
  untilFilter.value = ''
}

async function runArchivingNow() {
  archivingNow.value = true
  try {
    const res = await runArchiveNow()
    toast.show(`Archived ${res.archived} clip(s)`)
    await loadGroups()
  } catch {
    toast.show('Could not run archiving', true)
  } finally {
    archivingNow.value = false
  }
}

const hasActiveFilters = computed(() => cameraFilter.value !== 'all' || !!sinceFilter.value || !!untilFilter.value)

function archiveLabel(path: string): string {
  if (!path) return 'Unknown archive'
  return path.split(/[/\\]/).pop() || path
}

// Grouped by camera within each expanded archive, matching how the ZIP
// itself is organized on disk (see archiver.py's arcname:
// "<camera>/<file>") and how Google Drive backups are now organized too
// (<date>/<camera>/<file>, see gdrive_queue.py) -- one consistent mental
// model everywhere clips show up as more than a flat list.
function sortedForGrouping(clips: ClipListItem[]): ClipListItem[] {
  return [...clips].sort((a, b) => a.camera.localeCompare(b.camera) || b.timestamp.localeCompare(a.timestamp))
}

function cameraCount(clips: ClipListItem[], camera: string): number {
  return clips.filter((c) => c.camera === camera).length
}

const pagedGroups = computed(() => allGroups.value.slice(first.value, first.value + GROUPS_PER_PAGE))

// If the current page's groups all just got removed (e.g. deleting the
// last clip in the last archive on the last page), step back rather than
// showing an empty page with a non-zero Paginator position.
watch(allGroups, () => {
  if (first.value > 0 && first.value >= allGroups.value.length) {
    first.value = Math.max(0, first.value - GROUPS_PER_PAGE)
  }
})

async function toggleArchive(path: string) {
  const next = new Set(expandedArchives.value)
  if (next.has(path)) {
    next.delete(path)
    expandedArchives.value = next
    return
  }
  next.add(path)
  expandedArchives.value = next
  if (clipsByArchive.value[path] || loadingArchive.value[path]) return

  loadingArchive.value = { ...loadingArchive.value, [path]: true }
  archiveLoadError.value = { ...archiveLoadError.value, [path]: false }
  try {
    // Matches the camera filter already narrowing this archive's
    // clip_count/total_size in the summary above -- without this, expanding
    // a filtered archive would show every camera's clips again, silently
    // contradicting the numbers right above it.
    const rows = await listClips({
      archived: true,
      archivePath: path,
      camera: cameraFilter.value === 'all' ? undefined : cameraFilter.value,
      sort: 'newest',
      limit: 500,
    })
    clipsByArchive.value = { ...clipsByArchive.value, [path]: rows }
  } catch {
    archiveLoadError.value = { ...archiveLoadError.value, [path]: true }
  } finally {
    loadingArchive.value = { ...loadingArchive.value, [path]: false }
  }
}

async function removeClip(clip: ClipListItem, archivePath: string) {
  const question = clip.gdrive_backed_up
    ? 'Delete this clip? This also removes its Google Drive backup.'
    : 'Delete this clip?'
  if (!(await confirm(question))) return
  deletingId.value = clip.id
  try {
    await deleteClip(clip.id)
    toast.show('Clip deleted')
    clipsByArchive.value = {
      ...clipsByArchive.value,
      [archivePath]: (clipsByArchive.value[archivePath] || []).filter((c) => c.id !== clip.id),
    }
    // Patch the already-loaded group counts locally instead of re-fetching
    // (which would also reset pagination back to page 1) -- we already
    // know exactly what changed.
    const group = allGroups.value.find((g) => g.archive_path === archivePath)
    if (group) {
      group.clip_count -= 1
      group.total_size -= clip.size_bytes || 0
      if (group.clip_count <= 0) {
        allGroups.value = allGroups.value.filter((g) => g.archive_path !== archivePath)
      }
    }
  } catch {
    toast.show('Could not delete clip', true)
  } finally {
    deletingId.value = null
  }
}
</script>

<template>
  <div class="archived-clips-section">
    <Message severity="info" :closable="false" class="archived-note">
      Archived clips are stored in compressed monthly ZIP files and can't be played directly from here — delete them
      from this list, or back them up to Google Drive below. Grouped below by the ZIP file each clip lives in.
    </Message>

    <div class="archive-filters">
      <Select
        v-model="cameraFilter"
        :options="['all', ...cameras]"
        class="archive-filter-camera"
        aria-label="Filter by camera"
      >
        <template #value="{ value }">{{ value === 'all' ? 'All cameras' : value }}</template>
        <template #option="{ option }">{{ option === 'all' ? 'All cameras' : option }}</template>
      </Select>
      <label class="archive-filter-date">
        <span class="sr-only">From date</span>
        <input v-model="sinceFilter" type="date" aria-label="From date" />
      </label>
      <label class="archive-filter-date">
        <span class="sr-only">To date</span>
        <input v-model="untilFilter" type="date" aria-label="To date" />
      </label>
      <Button
        v-if="hasActiveFilters"
        size="small"
        severity="secondary"
        text
        label="Clear filters"
        @click="clearFilters"
      />
      <Button
        size="small"
        severity="secondary"
        outlined
        class="run-archive-now"
        :loading="archivingNow"
        :disabled="archivingNow"
        label="Run Archiving Now"
        @click="runArchivingNow"
      />
    </div>

    <div v-if="loading" class="archived-status"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" :closable="false">Failed to load archived clips.</Message>
    <Message v-else-if="!allGroups.length" severity="secondary" :closable="false">
      {{ hasActiveFilters ? 'No archives match these filters.' : 'No archived clips yet.' }}
    </Message>
    <template v-else>
      <Panel
        v-for="group in pagedGroups"
        :key="group.archive_path"
        class="archive-panel"
        toggleable
        :collapsed="!expandedArchives.has(group.archive_path)"
        @update:collapsed="() => toggleArchive(group.archive_path)"
      >
        <template #header>
          <!-- NOSONAR: prose explaining a deliberate design choice below,
            not commented-out code.
            PrimeVue's own toggle button (rendered after this #header slot,
            as a sibling — see p-panel-header-actions) only wires *itself*
            up as clickable, not the rest of the header — clicking anywhere
            else in the header row would otherwise silently do nothing.
            role="button" + the click/keydown handlers here make the whole
            row toggle, which is what people actually expect to be able to
            click; the real toggle button still works independently since
            it isn't nested inside this element. A real <button> can't be
            used for the whole row without nesting an interactive element
            inside another (invalid HTML) once PrimeVue's own toggle
            button renders alongside it.
          -->
          <div
            class="archive-panel-header"
            role="button"
            tabindex="0"
            :aria-expanded="expandedArchives.has(group.archive_path)"
            @click="toggleArchive(group.archive_path)"
            @keydown.enter="toggleArchive(group.archive_path)"
            @keydown.space.prevent="toggleArchive(group.archive_path)"
          >
            <Tag severity="secondary" value="ZIP" />
            <span class="archive-name">{{ archiveLabel(group.archive_path) }}</span>
            <span class="archive-meta"
              >{{ group.clip_count }} clip{{ group.clip_count === 1 ? '' : 's' }} ·
              {{ fmtSize(group.total_size) }}</span
            >
          </div>
        </template>

        <div v-if="loadingArchive[group.archive_path]" class="archived-status"><LoadingIndicator /></div>
        <Message v-else-if="archiveLoadError[group.archive_path]" severity="error" :closable="false">
          Failed to load clips in this archive.
        </Message>
        <!-- NOSONAR: prose, not commented-out code.
          Grouped by camera (row-group-mode="subheader" needs its rows
          pre-sorted so same-camera rows are contiguous, hence
          sortedForGrouping rather than the raw fetched order) -- the
          "Camera" column itself is dropped since the group header already
          says it once per group instead of on every single row.
        -->
        <DataTable
          v-else
          :value="sortedForGrouping(clipsByArchive[group.archive_path] ?? [])"
          data-key="id"
          size="small"
          row-group-mode="subheader"
          group-rows-by="camera"
        >
          <template #groupheader="{ data }">
            <span class="camera-group-header">
              {{ data.camera }}
              <span class="archive-meta"
                >({{ cameraCount(clipsByArchive[group.archive_path] ?? [], data.camera) }} clip{{
                  cameraCount(clipsByArchive[group.archive_path] ?? [], data.camera) === 1 ? '' : 's'
                }})</span
              >
            </span>
          </template>
          <Column field="timestamp" header="Date">
            <template #body="{ data }">{{ fmtTs(data.timestamp) }}</template>
          </Column>
          <Column field="size_bytes" header="Size">
            <template #body="{ data }">{{ fmtSize(data.size_bytes) }}</template>
          </Column>
          <Column header="Drive Backup">
            <template #body="{ data }">
              <Tag v-if="data.gdrive_backed_up" severity="success" value="Backed up" />
              <Tag v-else severity="secondary" value="Not backed up" />
            </template>
          </Column>
          <Column header="">
            <template #body="{ data }">
              <Button
                size="small"
                severity="danger"
                text
                label="Delete"
                :disabled="deletingId === data.id"
                @click="removeClip(data, group.archive_path)"
              />
            </template>
          </Column>
        </DataTable>
      </Panel>

      <Paginator
        v-if="allGroups.length > GROUPS_PER_PAGE"
        v-model:first="first"
        :rows="GROUPS_PER_PAGE"
        :total-records="allGroups.length"
        class="archive-paginator"
      />
    </template>
  </div>
</template>

<style scoped>
.archived-clips-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.archived-note {
  margin: 0;
}

.archive-filters {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.archive-filter-camera {
  min-width: 10rem;
}

.run-archive-now {
  margin-left: auto;
}

.archive-filter-date input {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  padding: 0.45rem 0.6rem;
  font-size: 0.85rem;
  /* Prevents a native date input's intrinsic width from forcing this row
     (and therefore the page) wider than a narrow/mobile viewport. */
  max-width: 9.5rem;
}

.archived-status {
  padding: 1rem;
}

.archive-panel-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  min-width: 0;
  cursor: pointer;
  border-radius: 6px;
}

.archive-panel-header:hover,
.archive-panel-header:focus-visible {
  background: var(--card-hover);
}

.camera-group-header {
  font-weight: 600;
}

.archive-name {
  font-weight: 600;
  word-break: break-all;
}

.archive-meta {
  color: var(--muted);
  font-size: 0.82rem;
}

.archive-paginator {
  display: flex;
  justify-content: center;
  /* PrimeVue's Paginator lays out its page-number buttons in a single row
     with no built-in wrap -- on a narrow/mobile viewport that overflows
     rather than shrinking, so it needs to be allowed to wrap here instead. */
  flex-wrap: wrap;
}
</style>
