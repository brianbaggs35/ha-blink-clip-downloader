<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import { fmtOffset, groupMatch, QUALITY_LABEL, QUALITY_SEVERITY, qualityLevel, type FoundFace } from './found'

const props = defineProps<{
  faces: FoundFace[]
  /** Candidate ids grouped by apparent person, from the server. */
  groups: string[][]
}>()
const selected = defineModel<string[]>('selected', { required: true })
const emit = defineEmits<{ 'add-to': [name: string, ids: string[]] }>()

const showLowQuality = ref(false)
// Hiding low-quality faces again also unpicks them: nothing out of sight
// should be enrolled by surprise.
watch(showLowQuality, (show) => {
  if (!show) selected.value = selected.value.filter((id) => qualityLevel(byId.value.get(id)?.quality ?? 1) !== 'low')
})

interface DisplayGroup {
  key: string
  faces: FoundFace[]
  visible: FoundFace[]
  match: string | null
  sources: number
}

const byId = computed(() => new Map(props.faces.map((face) => [face.id, face])))

// Grouping arrives separately from the faces themselves, so a face the
// server has not grouped yet (or ever) still gets shown, in its own group.
const displayGroups = computed<DisplayGroup[]>(() => {
  const grouped = new Set<string>()
  const groups: FoundFace[][] = []
  for (const ids of props.groups) {
    const faces = ids.map((id) => byId.value.get(id)).filter((face): face is FoundFace => !!face)
    faces.forEach((face) => grouped.add(face.id))
    if (faces.length) groups.push(faces)
  }
  for (const face of props.faces) if (!grouped.has(face.id)) groups.push([face])
  return groups.map((faces) => {
    const ordered = [...faces].sort((a, b) => b.quality - a.quality)
    return {
      key: ordered[0].id,
      faces: ordered,
      visible: showLowQuality.value ? ordered : ordered.filter((f) => qualityLevel(f.quality) !== 'low'),
      match: groupMatch(ordered),
      sources: new Set(ordered.map((f) => (f.source.kind === 'clip' ? f.source.clipId : f.source.label))).size,
    }
  })
})

const shownGroups = computed(() => displayGroups.value.filter((g) => g.visible.length))
const lowQualityCount = computed(() => props.faces.filter((f) => qualityLevel(f.quality) === 'low').length)
const selectedSet = computed(() => new Set(selected.value))

function toggle(id: string) {
  selected.value = selectedSet.value.has(id) ? selected.value.filter((s) => s !== id) : [...selected.value, id]
}

function groupFullySelected(group: DisplayGroup): boolean {
  return group.visible.every((face) => selectedSet.value.has(face.id))
}

function toggleGroup(group: DisplayGroup) {
  const ids = group.visible.map((face) => face.id)
  selected.value = groupFullySelected(group)
    ? selected.value.filter((id) => !ids.includes(id))
    : [...new Set([...selected.value, ...ids])]
}

function caption(face: FoundFace): string {
  if (face.source.kind === 'photo') return face.source.label
  return face.time == null ? face.source.camera : `${face.source.camera} · ${fmtOffset(face.time)}`
}
</script>

<template>
  <div class="found-faces">
    <div class="found-faces-toolbar">
      <span class="field-label">
        {{ faces.length }} face{{ faces.length === 1 ? '' : 's' }} found, grouped by who they look like. Pick the ones
        that are the person you're enrolling — several angles and lighting conditions help most.
      </span>
      <div v-if="lowQualityCount" class="low-quality-toggle">
        <ToggleSwitch v-model="showLowQuality" input-id="biometrics-show-low-quality" />
        <label for="biometrics-show-low-quality" class="field-label">
          Show {{ lowQualityCount }} low-quality face{{ lowQualityCount === 1 ? '' : 's' }}
        </label>
      </div>
    </div>

    <p v-if="!shownGroups.length" class="field-label">
      Every face found so far is too small or blurred to be a useful reference — turn on "Show low-quality faces" to see
      them anyway, or scan clips where someone comes closer to the camera.
    </p>

    <section v-for="(group, index) in shownGroups" :key="group.key" class="face-group">
      <header class="face-group-header">
        <strong>{{ group.match ? `Already recognized as ${group.match}` : `Person ${index + 1}` }}</strong>
        <span class="field-label">
          {{ group.faces.length }} face{{ group.faces.length === 1 ? '' : 's' }} from {{ group.sources }}
          {{ group.sources === 1 ? 'source' : 'sources' }}
        </span>
        <span class="face-group-actions">
          <Button
            v-if="group.match"
            size="small"
            text
            :label="`Add to ${group.match}`"
            @click="
              emit(
                'add-to',
                group.match,
                group.visible.map((f) => f.id),
              )
            "
          />
          <Button
            size="small"
            text
            severity="secondary"
            :label="groupFullySelected(group) ? 'Deselect all' : 'Select all'"
            @click="toggleGroup(group)"
          />
        </span>
      </header>
      <div class="face-tiles">
        <button
          v-for="face in group.visible"
          :key="face.id"
          type="button"
          class="face-tile"
          :class="{ selected: selectedSet.has(face.id) }"
          :aria-pressed="selectedSet.has(face.id)"
          :aria-label="`${QUALITY_LABEL[qualityLevel(face.quality)]} face, ${caption(face)}`"
          @click="toggle(face.id)"
        >
          <img :src="face.thumbnail" alt="" />
          <span v-if="selectedSet.has(face.id)" class="face-check" aria-hidden="true">✓</span>
          <Tag
            class="face-quality"
            :severity="QUALITY_SEVERITY[qualityLevel(face.quality)]"
            :value="QUALITY_LABEL[qualityLevel(face.quality)]"
          />
          <span class="face-caption">{{ caption(face) }}</span>
          <span v-if="face.match && face.match.name !== group.match" class="face-caption face-match">
            Recognized as {{ face.match.name }}
          </span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.found-faces {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.found-faces-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem 1rem;
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
  margin: 0;
}

.low-quality-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.face-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--border);
}

.face-group-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem 0.75rem;
}

.face-group-actions {
  margin-left: auto;
  display: flex;
  gap: 0.25rem;
}

.face-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(112px, 100%), 1fr));
  gap: 0.6rem;
}

.face-tile {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0 0 0.4rem;
  border: 2px solid transparent;
  border-radius: var(--radius-sm, 10px);
  overflow: hidden;
  background: var(--card2);
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.face-tile:hover,
.face-tile:focus-visible {
  border-color: var(--border-strong);
}

.face-tile.selected {
  border-color: var(--accent);
}

.face-tile img {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  display: block;
}

.face-check {
  position: absolute;
  top: 0.35rem;
  right: 0.35rem;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.8rem;
}

.face-quality {
  position: absolute;
  top: 0.35rem;
  left: 0.35rem;
  font-size: 0.65rem;
}

.face-caption {
  padding: 0 0.45rem;
  font-size: 0.7rem;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.face-match {
  color: var(--warn);
}
</style>
