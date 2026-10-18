<script setup lang="ts">
import { computed } from 'vue'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import type { RestoreReport } from '../../api/types'
import { clipNotes, fileStatusSeverity, fileStatusText, nonEmpty, plural, totalAdded } from './words'

// What a restore did, and the checks run on the result afterwards. The
// checks are the verification: that every record in the backup was either
// restored or deliberately left alone, that the clips it added are really in
// the library, and that the files written read back the way they were stored.

const props = defineProps<{ report: RestoreReport; clipFolder: string }>()

const tables = computed(() => nonEmpty(props.report.tables))
const notes = computed(() => clipNotes(props.report.clips, props.clipFolder))
const added = computed(() => totalAdded(props.report.tables))
</script>

<template>
  <div class="restore-report">
    <Message v-if="report.verified" severity="success" :closable="false" class="report-verdict">
      Restore complete and verified — {{ plural(added, 'record') }} added.
    </Message>
    <Message v-else severity="warn" :closable="false" class="report-verdict">
      The restore finished, but a check below did not pass. Nothing was lost, and restoring the same backup again only
      adds what is still missing.
    </Message>

    <h4 class="report-heading">Verification</h4>
    <ul class="check-list">
      <li v-for="check in report.checks" :key="check.label" :class="{ failed: !check.ok }">
        <i :class="check.ok ? 'pi pi-check-circle ok' : 'pi pi-times-circle bad'" aria-hidden="true" />
        <span>
          {{ check.label }}
          <span v-if="check.detail" class="muted-note">— {{ check.detail }}</span>
        </span>
      </li>
    </ul>

    <h4 class="report-heading">What was added</h4>
    <table class="report-table">
      <thead>
        <tr>
          <th scope="col">Records</th>
          <th scope="col">Added</th>
          <th scope="col">Already here</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="table in tables" :key="table.name" :data-table="table.name">
          <th scope="row">{{ table.label }}</th>
          <td data-label="Added" class="added">{{ table.added.toLocaleString() }}</td>
          <td data-label="Already here">{{ table.skipped.toLocaleString() }}</td>
        </tr>
      </tbody>
    </table>
    <ul v-if="notes.length" class="clip-notes">
      <li v-for="note in notes" :key="note">{{ note }}</li>
    </ul>

    <template v-if="report.files.length">
      <h4 class="report-heading">Settings and files</h4>
      <ul class="file-list">
        <li v-for="file in report.files" :key="file.label" :data-file-status="file.status">
          <span class="file-label">{{ file.label }}</span>
          <Tag :severity="fileStatusSeverity(file.status)" :value="fileStatusText(file.status)" />
          <span v-if="file.detail" class="muted-note file-detail">{{ file.detail }}</span>
        </li>
      </ul>
    </template>

    <Message v-if="report.restart_needed" severity="info" :closable="false" class="restart-note">
      <strong>Restart the add-on to use the restored settings:</strong> Settings → Add-ons → Blink Clip Downloader →
      Restart. The records above are already in the library; only the settings are read at start-up.
    </Message>
  </div>
</template>

<style scoped>
.restore-report {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  container-type: inline-size;
}
.report-heading {
  margin: 0.6rem 0 0;
  font-size: 0.9rem;
  font-weight: 600;
}
.check-list,
.file-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.check-list li {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
}
.check-list .ok {
  color: var(--green, #2f9e44);
}
.check-list .bad {
  color: var(--red, #e03131);
}
.report-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}
.report-table th,
.report-table td {
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--border);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.report-table th:first-child {
  text-align: left;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.report-table thead th {
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 600;
}
.report-table .added {
  font-weight: 700;
}
@container (max-width: 520px) {
  .report-table,
  .report-table tbody,
  .report-table tr,
  .report-table th,
  .report-table td {
    display: block;
  }
  .report-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .report-table tr {
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--border);
  }
  .report-table th,
  .report-table td {
    border: 0;
    padding: 0.1rem 0.25rem;
  }
  .report-table td {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .report-table td::before {
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
.report-verdict :deep(.p-message-text),
.restart-note :deep(.p-message-text) {
  color: var(--text);
}
.muted-note {
  color: var(--muted);
  font-size: 0.82rem;
}
</style>
