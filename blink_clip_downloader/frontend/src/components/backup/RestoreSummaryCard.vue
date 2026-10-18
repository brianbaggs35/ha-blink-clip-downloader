<script setup lang="ts">
import { computed } from 'vue'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import type { RestoreSummary } from '../../api/types'
import { clipNotes, clipsAdded, fileStatusSeverity, fileStatusText, nonEmpty, plural, totalAdded } from './words'

// What is in the backup and what restoring it would do, shown before
// anything is changed. The numbers are not an estimate: the add-on ran the
// real restore without writing, so what is promised here is what is done.

const props = defineProps<{ summary: RestoreSummary }>()
const replaceSettings = defineModel<boolean>('replaceSettings', { default: false })

const tables = computed(() => nonEmpty(props.summary.tables))
const notes = computed(() => clipNotes(props.summary.clips, props.summary.clip_folder))
const added = computed(() => totalAdded(props.summary.tables))
const clips = computed(() => clipsAdded(props.summary.tables))
// Only a file that is already here can be replaced, so only then is there a
// choice to offer.
const canReplace = computed(() => props.summary.files.some((file) => file.status === 'exists'))
const nothingToAdd = computed(() => added.value === 0 && !props.summary.files.some((f) => f.status !== 'exists'))
const faces = computed(() => tables.value.find((table) => table.name === 'face_enrollments'))

function made(iso: string): string {
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString()
}
</script>

<template>
  <div class="restore-summary">
    <p class="summary-source">
      Backup made <strong>{{ made(summary.created_at) }}</strong> by version {{ summary.addon_version }}.
      <span class="verified"
        ><i class="pi pi-check-circle" aria-hidden="true" /> All {{ summary.verified_parts }} parts checked against
        their checksums.</span
      >
    </p>

    <Message v-if="nothingToAdd" severity="info" size="small" :closable="false" class="nothing-to-add">
      Everything in this backup is already here, so restoring it would change nothing.
    </Message>

    <h4 class="summary-heading">What restoring will do</h4>
    <!-- The roles are explicit because on a narrow card the table is laid out as
         stacked rows (see the container query below), and a browser drops a
         table's semantics from an element once its display is no longer a table's. -->
    <table class="summary-table" role="table">
      <thead role="rowgroup">
        <tr role="row">
          <th scope="col" role="columnheader">Records</th>
          <th scope="col" role="columnheader">In the backup</th>
          <th scope="col" role="columnheader">Will be added</th>
          <th scope="col" role="columnheader">Already here</th>
        </tr>
      </thead>
      <tbody role="rowgroup">
        <tr v-for="table in tables" :key="table.name" :data-table="table.name" role="row">
          <th scope="row" role="rowheader">{{ table.label }}</th>
          <td role="cell" data-label="In the backup">{{ table.rows.toLocaleString() }}</td>
          <td role="cell" data-label="Will be added" class="added">{{ table.added.toLocaleString() }}</td>
          <td role="cell" data-label="Already here">{{ table.skipped.toLocaleString() }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!tables.length" class="muted-note">The backup holds no records, only files.</p>

    <ul v-if="notes.length" class="clip-notes">
      <li v-for="note in notes" :key="note">{{ note }}</li>
    </ul>
    <p v-if="clips > 0" class="muted-note">
      Whatever belongs to a clip that is already here — its analysis, detections and events — stays as it is and is not
      added a second time.
    </p>

    <template v-if="summary.files.length">
      <h4 class="summary-heading">Settings and files</h4>
      <ul class="file-list">
        <li v-for="file in summary.files" :key="file.label" :data-file-status="file.status">
          <span class="file-label">{{ file.label }}</span>
          <Tag :severity="fileStatusSeverity(file.status)" :value="fileStatusText(file.status)" />
          <span v-if="file.detail" class="muted-note file-detail">{{ file.detail }}</span>
        </li>
      </ul>
      <div v-if="canReplace" class="replace-option">
        <ToggleSwitch v-model="replaceSettings" input-id="restore-replace-settings" />
        <label for="restore-replace-settings">
          <strong>Replace settings and pictures that are already here</strong>
          <span class="muted-note">
            Off by default, so what you have set up on this install is kept. Turn it on to use the backup's copies
            instead.
          </span>
        </label>
      </div>
    </template>

    <Message v-if="faces" severity="secondary" size="small" :closable="false" class="faces-note">
      This backup includes enrolled faces. {{ plural(faces.added, 'face') }} will be added; faces already enrolled here
      are not enrolled twice.
    </Message>

    <p v-if="added > 0" class="total-line">
      <strong>{{ plural(added, 'record') }}</strong> will be added, <strong>{{ plural(clips, 'clip') }}</strong> of
      them. Nothing that is already here is overwritten or deleted.
    </p>
  </div>
</template>

<style scoped>
.restore-summary {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  /* The narrow layout follows the card's own width, not the screen's: next to
     the sidebar a tablet leaves the card barely wider than a phone. */
  container-type: inline-size;
}
.summary-source {
  margin: 0;
}
.verified {
  display: inline-block;
  margin-left: 0.4rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.verified .pi {
  color: var(--green, #2f9e44);
}
.summary-heading {
  margin: 0.6rem 0 0;
  font-size: 0.9rem;
  font-weight: 600;
}
.summary-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}
.summary-table th,
.summary-table td {
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--border);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.summary-table th:first-child {
  text-align: left;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.summary-table thead th {
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 600;
}
/* Bold, in the text colour: the accent colour is under the 4.5:1 small text
   needs on the card background. */
.summary-table .added {
  font-weight: 700;
}
/* Four columns of big numbers cannot fit a phone: "1,234,567" alone is wider
   than a quarter of it. Stack each record kind as a label with its three
   figures beneath, the header row kept for screen readers. */
@container (max-width: 520px) {
  .summary-table,
  .summary-table tbody,
  .summary-table tr,
  .summary-table th,
  .summary-table td {
    display: block;
  }
  .summary-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .summary-table tr {
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--border);
  }
  .summary-table th,
  .summary-table td {
    border: 0;
    padding: 0.1rem 0.25rem;
  }
  .summary-table td {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .summary-table td::before {
    content: attr(data-label);
    color: var(--muted);
    font-size: 0.82rem;
  }
}
.clip-notes {
  margin: 0;
  padding-left: 1.2rem;
  font-size: 0.88rem;
  /* A long folder path has no spaces to break at. */
  overflow-wrap: anywhere;
}
.file-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.file-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
.file-label {
  flex: 1 1 14rem;
  overflow-wrap: anywhere;
}
.file-detail {
  flex-basis: 100%;
}
.replace-option {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  margin-top: 0.3rem;
}
.replace-option :deep(.p-toggleswitch) {
  flex: none;
  margin-top: 0.1rem;
}
.replace-option label {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  cursor: pointer;
}
.total-line {
  margin: 0.4rem 0 0;
}
/* PrimeVue's secondary/info text is under the 4.5:1 small text needs on some
   themes; the theme's own text colour is fine on both. */
.nothing-to-add :deep(.p-message-text),
.faces-note :deep(.p-message-text) {
  color: var(--text);
}
.muted-note {
  color: var(--muted);
  font-size: 0.82rem;
}
</style>
