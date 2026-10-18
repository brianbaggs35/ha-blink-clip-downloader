<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import FileUpload, { type FileUploadSelectEvent } from 'primevue/fileupload'
import Message from 'primevue/message'
import ProgressBar from 'primevue/progressbar'
import { discardBackupUpload, inspectBackup, restoreBackup, uploadBackupFile } from '../../api/backup'
import { ApiError, describeApiError } from '../../api/client'
import type { RestoreReport, RestoreSummary } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useRefreshStore } from '../../stores/refresh'
import RestoreReportCard from './RestoreReportCard.vue'
import RestoreSummaryCard from './RestoreSummaryCard.vue'
import { clipsAdded, plural, totalAdded } from './words'

// Restoring is four deliberate steps, none of them automatic: choose the
// file, look at what is in it and what restoring would do, confirm, and read
// what was done. The add-on refuses to restore a file nobody has looked at,
// so the summary cannot be skipped even by a stray request.

type Phase = 'idle' | 'uploading' | 'inspecting' | 'review' | 'restoring' | 'done'

const confirm = useConfirm()
const refresh = useRefreshStore()

const phase = ref<Phase>('idle')
const fileName = ref('')
const progress = ref(0)
const error = ref('')
const summary = ref<RestoreSummary | null>(null)
const report = ref<RestoreReport | null>(null)
const replaceSettings = ref(false)
const picker = ref<InstanceType<typeof FileUpload> | null>(null)

// What the add-on holds this restore under, and the means of stopping a
// send that is under way.
let uploadId = ''
let sending: AbortController | null = null

function reset() {
  phase.value = 'idle'
  summary.value = null
  report.value = null
  replaceSettings.value = false
  uploadId = ''
}

function cancelled(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

async function look(file: File) {
  fileName.value = file.name
  progress.value = 0
  phase.value = 'uploading'
  sending = new AbortController()
  try {
    uploadId = await uploadBackupFile(
      file,
      (sent, total) => {
        progress.value = Math.round((sent / total) * 100)
      },
      sending.signal,
    )
    phase.value = 'inspecting'
    summary.value = await inspectBackup(uploadId)
    phase.value = 'review'
  } catch (e) {
    if (uploadId) await discardBackupUpload(uploadId).catch(() => undefined)
    if (!cancelled(e)) error.value = describeApiError(e, 'Could not read that file.')
    reset()
  } finally {
    sending = null
  }
}

function onSelect(event: FileUploadSelectEvent) {
  const file = (event.files as File[] | undefined)?.[0]
  // Cleared at once so the same file can be chosen again after a mistake.
  picker.value?.clear()
  error.value = ''
  if (!file) return
  if (!/\.zip$/i.test(file.name)) {
    error.value = 'That is not a .zip file. Choose the backup file this add-on made.'
    return
  }
  return look(file)
}

function confirmationText(s: RestoreSummary): string {
  const kept = replaceSettings.value ? ', apart from the settings and pictures you chose to replace.' : '.'
  return (
    `This adds ${plural(totalAdded(s.tables), 'record')} to this install, ` +
    `${plural(clipsAdded(s.tables), 'clip')} of them. ` +
    `Nothing that is already here is overwritten or deleted${kept}`
  )
}

async function restore() {
  if (!summary.value) return
  if (!(await confirm(confirmationText(summary.value), 'Restore this backup?'))) return
  phase.value = 'restoring'
  error.value = ''
  try {
    report.value = await restoreBackup(uploadId, replaceSettings.value)
    uploadId = ''
    phase.value = 'done'
    // Every tab that lists clips or people has new rows to show.
    refresh.bump()
  } catch (e) {
    error.value = describeApiError(e, 'The restore failed. The add-on logs have the reason.')
    // An upload the add-on no longer holds cannot be tried again.
    if (e instanceof ApiError && e.status === 404) reset()
    else phase.value = 'review'
  }
}

async function cancel() {
  if (phase.value === 'uploading') {
    sending?.abort()
    return
  }
  const id = uploadId
  reset()
  await discardBackupUpload(id).catch(() => undefined)
}

// Leaving the tab ends a restore that has not begun: stop a send, and do not
// leave a checked file waiting on the add-on for an hour.
onBeforeUnmount(() => {
  sending?.abort()
  if (phase.value === 'review') void discardBackupUpload(uploadId).catch(() => undefined)
})
</script>

<template>
  <Card class="restore-card">
    <template #title>Restore from a backup</template>
    <template #subtitle>
      Choose a backup this add-on made. You will see exactly what it holds and what restoring will do before anything
      changes. Restoring only adds what is missing — it never overwrites or deletes what you have.
    </template>
    <template #content>
      <Message v-if="error" severity="error" size="small" :closable="false" class="restore-error">{{ error }}</Message>

      <div v-show="phase === 'idle'" class="restore-choose">
        <!-- No `accept` here, deliberately: the picker would turn a wrong file away
             without a word and go on saying "No file chosen". onSelect says what
             is wrong instead. -->
        <FileUpload
          ref="picker"
          mode="basic"
          size="small"
          :auto="false"
          custom-upload
          choose-label="Choose backup file…"
          @select="onSelect"
        />
        <p class="muted-note">
          Do this as early as you can after reinstalling: a clip the add-on has already downloaded again keeps its own
          record rather than the backup's.
        </p>
      </div>

      <div v-if="phase === 'uploading'" class="restore-progress">
        <p>
          Sending <strong>{{ fileName }}</strong
          >…
        </p>
        <ProgressBar :value="progress" />
        <Button label="Cancel" severity="secondary" size="small" outlined @click="cancel" />
      </div>

      <p v-if="phase === 'inspecting'" class="restore-working">
        Checking <strong>{{ fileName }}</strong
        >…
      </p>

      <template v-if="phase === 'review' && summary">
        <RestoreSummaryCard v-model:replace-settings="replaceSettings" :summary="summary" />
        <div class="restore-actions">
          <Button label="Restore this backup" icon="pi pi-history" @click="restore" />
          <Button label="Cancel" severity="secondary" outlined @click="cancel" />
        </div>
      </template>

      <p v-if="phase === 'restoring'" class="restore-working">Restoring… this can take a minute for a large backup.</p>

      <template v-if="phase === 'done' && report && summary">
        <RestoreReportCard :report="report" :clip-folder="summary.clip_folder" />
        <div class="restore-actions">
          <Button label="Restore another backup" severity="secondary" outlined @click="reset" />
        </div>
      </template>
    </template>
  </Card>
</template>

<style scoped>
.restore-card {
  container-type: inline-size;
}
.restore-choose {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  align-items: flex-start;
}
.restore-choose p {
  margin: 0;
}
.restore-progress {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  align-items: flex-start;
}
.restore-progress p {
  margin: 0;
}
.restore-progress :deep(.p-progressbar) {
  width: 100%;
}
.restore-working {
  margin: 0;
}
.restore-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  margin-top: 1rem;
}
.restore-error {
  margin-bottom: 0.8rem;
}
.restore-error :deep(.p-message-text) {
  color: var(--text);
}
.muted-note {
  color: var(--muted);
  font-size: 0.82rem;
}
</style>
