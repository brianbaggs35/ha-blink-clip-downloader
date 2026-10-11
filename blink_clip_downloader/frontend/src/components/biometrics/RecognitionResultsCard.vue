<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Card from 'primevue/card'
import Message from 'primevue/message'
import { getFaceRecognitionStats } from '../../api/faces'
import type { FaceHeldBackReason, FaceRecognitionStats } from '../../api/types'
import { useRefreshStore } from '../../stores/refresh'

// What face recognition made of the last few weeks of analyzed clips: how
// many were recognized, how many faces matched, and why the rest did not
// count. Counts only, never who (see face_outcomes.py) — the people
// themselves are on their cards above and in the activity card below.
//
// It is here so a household can see *why* a clip wasn't recognized without
// reading logs, and above all whether the Ignore Unclear Faces setting would
// change anything for them: a row only appears when there is something in it.
interface Reason {
  key: FaceHeldBackReason
  label: string
  /** `ignoring` is whether Ignore Unclear Faces is on right now. */
  hint: (ignoring: boolean) => string
}

// In the order the server lists them (face_outcomes.py's HELD_BACK_OUTCOMES).
const REASONS: Reason[] = [
  {
    key: 'unfamiliar',
    label: 'A clear face matched nobody',
    hint: () => "A visitor, or someone who isn't enrolled yet. Find them in one of these clips above to enroll them.",
  },
  {
    key: 'unclear_ignorable',
    label: 'Unclear faces matched nobody',
    hint: (ignoring) =>
      ignoring
        ? 'Analyzed before Ignore Unclear Faces was turned on — newer clips like these now count as recognized.'
        : "Blurred, cut off or small, with a household member right beside them. Turn on Ignore Unclear Faces in the add-on's Configuration tab to count these as recognized.",
  },
  {
    key: 'unclear',
    label: 'Unclear faces with no recognized person to put them down to',
    hint: () =>
      'Too blurred, cut off or small to tell, and no household member was seen nearby for each of them to be.',
  },
  {
    key: 'not_approved',
    label: "Someone enrolled isn't approved",
    hint: () => 'Approve them on their card above if they should clear alerts.',
  },
  {
    key: 'extra_people',
    label: 'More people than faces recognized',
    hint: () =>
      "Object detection saw more people at once than household members were recognized — someone's face never showed.",
  },
  {
    key: 'unexamined',
    label: "A frame couldn't be examined",
    hint: () => "The face models couldn't run on part of the clip. The add-on log says why.",
  },
]

const stats = ref<FaceRecognitionStats | null>(null)
const loaded = ref(false)
const refresh = useRefreshStore()

// Nothing here is an unsaved edit a reload could clobber, so it reloads on
// every refresh tick (a camera rename, the Refresh button). The previous
// figures stay up until the new ones arrive rather than flashing away, and a
// failed reload keeps them: only with nothing to show does the card say it
// could not load.
let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  const result = await getFaceRecognitionStats().catch(() => null)
  if (seq !== loadSeq) return
  if (result) stats.value = result
  loaded.value = true
}
onMounted(load)
watch(() => refresh.tick, load)

// Hidden until it has something to say: with recognition off and nothing
// ever recorded, the page's own banner already says so.
const shown = computed(
  () => loaded.value && (stats.value === null || stats.value.recognition_enabled || stats.value.clips_checked > 0),
)

function plural(count: number, word: string): string {
  return count === 1 ? word : `${word}s`
}

function notRecognized(s: FaceRecognitionStats): number {
  return s.clips_with_faces - s.clips_recognized
}

function percentRecognized(s: FaceRecognitionStats): number {
  return Math.round((s.clips_recognized / s.clips_with_faces) * 100)
}

function heldBackRows(s: FaceRecognitionStats) {
  return REASONS.map((reason) => ({ ...reason, count: s.held_back[reason.key] })).filter((row) => row.count > 0)
}

function facesLine(s: FaceRecognitionStats): string {
  const seen = `${s.faces_seen} ${plural(s.faces_seen, 'face')} seen`
  const unmatched = s.faces_seen - s.faces_matched
  if (unmatched === 0) return `${seen}, all matched a household member.`
  const matched = `${s.faces_matched} matched a household member`
  const clear = unmatched - s.faces_unclear
  let nobody = `${unmatched} matched nobody`
  if (s.faces_unclear === unmatched) nobody += ', all of them unclear'
  else if (s.faces_unclear > 0) nobody += ` (${s.faces_unclear} unclear, ${clear} clear)`
  return `${seen}: ${matched}, ${nobody}.`
}
</script>

<template>
  <Card v-if="shown" class="recognition-results-card">
    <template #title>📊 Recognition results</template>
    <template #subtitle>
      What face recognition made of the last {{ stats?.days ?? 30 }} days of analyzed clips — how many faces matched
      someone enrolled, and why the others didn't count.
    </template>
    <template #content>
      <div v-if="!stats" class="muted-note">Failed to load recognition results.</div>
      <div v-else-if="stats.clips_checked === 0" class="muted-note">
        Nothing to show yet. This fills in as clips are analyzed with face recognition on.
      </div>
      <div v-else-if="stats.clips_with_faces === 0" class="muted-note">
        Face recognition checked {{ stats.clips_checked }} {{ plural(stats.clips_checked, 'clip') }} and saw no faces in
        any of them.
      </div>
      <template v-else>
        <dl class="results-tiles">
          <div class="results-tile">
            <dt>Clips with faces</dt>
            <dd>{{ stats.clips_with_faces }}</dd>
          </div>
          <div class="results-tile">
            <dt>Recognized</dt>
            <dd>
              {{ stats.clips_recognized }} <span class="muted-note">({{ percentRecognized(stats) }}%)</span>
            </dd>
          </div>
          <div class="results-tile">
            <dt>Not recognized</dt>
            <dd>{{ notRecognized(stats) }}</dd>
          </div>
        </dl>

        <p class="faces-line">{{ facesLine(stats) }}</p>
        <p class="muted-note">
          Each face is counted once for every sampled frame it appears in.
          <template v-if="stats.faces_unclear > 0">
            Unclear means blurred, cut off by the edge of the frame, small or turned away.
          </template>
        </p>

        <Message v-if="stats.clips_set_aside > 0" severity="info" size="small" :closable="false" class="set-aside">
          Ignore Unclear Faces counted {{ stats.clips_set_aside }} {{ plural(stats.clips_set_aside, 'clip') }} as
          recognized by setting unclear faces aside.
        </Message>

        <section v-if="heldBackRows(stats).length" class="results-reasons-section">
          <h3 class="results-heading">
            Why {{ notRecognized(stats) }} {{ plural(notRecognized(stats), 'clip') }} didn't count as recognized
          </h3>
          <ul class="results-reasons">
            <li v-for="row in heldBackRows(stats)" :key="row.key" :data-reason="row.key">
              <span class="reason-count">{{ row.count }}</span>
              <span class="reason-text">
                <strong>{{ row.label }}</strong>
                <span class="muted-note">{{ row.hint(stats.ignore_unclear_faces) }}</span>
              </span>
            </li>
          </ul>
        </section>
      </template>
    </template>
  </Card>
</template>

<style scoped>
.recognition-results-card {
  margin-bottom: 1rem;
  /* The narrow layout below follows the card's own width, not the screen's:
     next to the sidebar a 700px tablet leaves the card barely wider than a
     phone, and a phone's media query would never see it. */
  container-type: inline-size;
}
.results-tiles {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  margin: 0 0 0.9rem;
}
.results-tile {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.8rem;
}
.results-tile dt {
  font-size: 0.78rem;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.results-tile dd {
  margin: 0.1rem 0 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  column-gap: 0.35rem;
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--accent, #7c5cff);
}
.faces-line {
  margin: 0 0 0.2rem;
  font-size: 0.9rem;
}
.set-aside {
  margin-top: 0.8rem;
}
/* PrimeVue's info text (blue 500) is 3.9:1 on its dark-theme tint, under the
   4.5:1 small text needs. The theme's own text colour is fine on both. */
.set-aside :deep(.p-message-text) {
  color: var(--text);
}
.results-reasons-section {
  margin-top: 0.9rem;
  padding-top: 0.7rem;
  border-top: 1px solid var(--border);
}
.results-heading {
  margin: 0 0 0.6rem;
  font-size: 0.85rem;
  font-weight: 600;
}
.results-reasons {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.results-reasons li {
  display: flex;
  gap: 0.75rem;
  align-items: baseline;
}
.reason-count {
  flex: none;
  min-width: 2rem;
  font-weight: 700;
  font-size: 1.2rem;
  text-align: right;
  color: var(--accent, #7c5cff);
}
.reason-text {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
  font-size: 0.88rem;
  overflow-wrap: anywhere;
}
.muted-note {
  font-size: 0.78rem;
  color: var(--muted);
}

/* A card under ~460px wide (a phone, or a tablet beside the sidebar) cannot
   fit three figures side by side: "recognized" alone is wider than a third of
   it. Stack them as label-and-figure rows instead, and trim the card's own
   padding the way the people card above does. */
@container (max-width: 460px) {
  .recognition-results-card :deep(.p-card-body) {
    padding: 1rem;
  }
  .results-tiles {
    grid-template-columns: 1fr;
    gap: 0.4rem;
  }
  .results-tile {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.75rem;
    padding: 0.45rem 0.75rem;
  }
  .results-tile dt {
    font-size: 0.85rem;
  }
  .results-tile dd {
    margin: 0;
    font-size: 1.2rem;
  }
  .results-reasons li {
    gap: 0.6rem;
  }
}
</style>
