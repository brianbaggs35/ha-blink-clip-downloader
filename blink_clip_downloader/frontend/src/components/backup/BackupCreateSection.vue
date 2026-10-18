<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import ToggleSwitch from 'primevue/toggleswitch'
import { backupToGDrive, createBackup, getBackupPreview } from '../../api/backup'
import { describeApiError } from '../../api/client'
import { fmtSize } from '../../api/constants'
import type { BackupPreview } from '../../api/types'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import { saveBlob } from './saveBlob'

// Making a backup: what it will hold, then either a download or a copy in
// Google Drive. The records are read from one consistent snapshot of the
// library, so a backup made while clips are arriving is still one moment.

const DRIVE_HINTS = {
  not_connected: 'Connect Google Drive on the Storage tab to also save backups there.',
  no_folder: 'Choose a Google Drive folder on the Storage tab to also save backups there.',
} as const

const toast = useToastStore()
const refresh = useRefreshStore()

const preview = ref<BackupPreview | null>(null)
const loaded = ref(false)
const includeFaces = ref(true)
const working = ref<'download' | 'drive' | null>(null)
const done = ref('')
const error = ref('')

let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  const result = await getBackupPreview().catch(() => null)
  if (seq !== loadSeq) return
  if (result) preview.value = result
  loaded.value = true
}
onMounted(load)
watch(() => refresh.tick, load)

// Everything the template needs from the preview, worked out once. With
// faces switched off, the line for them goes too: the list is what the
// backup will hold.
const view = computed(() => {
  const shown = preview.value
  if (!shown) return null
  const state = shown.gdrive.state
  let driveHint = ''
  if (state === 'ready') driveHint = `Saves into ${shown.gdrive.folder} on your Google Drive.`
  else if (state !== 'unavailable') driveHint = DRIVE_HINTS[state]
  return {
    included: shown.tables.filter((table) => table.rows > 0 && (includeFaces.value || !table.biometric)),
    holdsFaces: shown.tables.some((table) => table.biometric && table.rows > 0),
    fileCount: shown.files.reduce((sum, file) => sum + file.items, 0),
    clipFolder: shown.clip_folder,
    driveState: state,
    driveHint,
  }
})

async function download(): Promise<string> {
  const { blob, filename } = await createBackup(includeFaces.value)
  saveBlob(blob, filename)
  return `Downloaded ${filename} (${fmtSize(blob.size)}). Keep it somewhere safe.`
}

async function sendToDrive(): Promise<string> {
  const saved = await backupToGDrive(includeFaces.value)
  return `Saved to Google Drive: ${saved.folder}/${saved.name} (${fmtSize(saved.size_bytes)}).`
}

async function run(kind: 'download' | 'drive') {
  working.value = kind
  done.value = ''
  error.value = ''
  try {
    done.value = await (kind === 'download' ? download() : sendToDrive())
    toast.show('Backup complete')
  } catch (e) {
    error.value = describeApiError(e, 'Could not create the backup. The add-on logs have the reason.')
  } finally {
    working.value = null
  }
}
</script>

<template>
  <Card class="backup-card">
    <template #title>Back up this install</template>
    <template #subtitle>
      One ZIP with your clip library and its AI analysis, what each camera has learned, and your settings — enough to
      pick up where you left off after reinstalling.
    </template>
    <template #content>
      <LoadingIndicator v-if="!loaded" />
      <Message v-else-if="!view" severity="warn" :closable="false">
        Could not read what a backup would hold. Reload the page to try again.
      </Message>
      <template v-else>
        <ul class="include-list">
          <li v-for="table in view.included" :key="table.name" :data-table="table.name">
            <span>{{ table.label }}</span>
            <strong>{{ table.rows.toLocaleString() }}</strong>
          </li>
          <li v-if="view.fileCount > 0" data-table="files">
            <span>Settings and reference pictures</span>
            <strong>{{ view.fileCount }}</strong>
          </li>
        </ul>
        <p v-if="!view.included.length && !view.fileCount" class="muted-note">Nothing to back up yet.</p>
        <p class="muted-note not-included">
          Not included: the clip videos, which are too large (they stay in <code>{{ view.clipFolder }}</code
          >), and every password and sign-in — Blink, Google Drive and this add-on's own — so you sign in again after
          restoring.
        </p>

        <div v-if="view.holdsFaces" class="faces-option">
          <ToggleSwitch v-model="includeFaces" input-id="backup-include-faces" />
          <label for="backup-include-faces">
            <strong>Include enrolled faces</strong>
            <span class="muted-note">
              Photos and face data for the people on the Biometrics tab. They never leave this device on their own —
              this puts them in the backup file, which is not encrypted, so keep it private.
            </span>
          </label>
        </div>

        <div class="backup-actions">
          <Button
            label="Download backup"
            icon="pi pi-download"
            :loading="working === 'download'"
            :disabled="working !== null"
            @click="run('download')"
          />
          <Button
            v-if="view.driveState !== 'unavailable'"
            label="Back up to Google Drive"
            icon="pi pi-cloud-upload"
            severity="secondary"
            :loading="working === 'drive'"
            :disabled="working !== null || view.driveState !== 'ready'"
            @click="run('drive')"
          />
        </div>
        <p v-if="view.driveHint" class="muted-note drive-hint">{{ view.driveHint }}</p>

        <Message v-if="working" severity="info" size="small" :closable="false" class="backup-working">
          Creating the backup… a large library can take a minute.
        </Message>
        <Message v-if="done" severity="success" size="small" :closable="false" class="backup-done">{{ done }}</Message>
        <Message v-if="error" severity="error" size="small" :closable="false" class="backup-error">{{ error }}</Message>
      </template>
    </template>
  </Card>
</template>

<style scoped>
.backup-card {
  container-type: inline-size;
}
.include-list {
  list-style: none;
  margin: 0 0 0.6rem;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(15rem, 100%), 1fr));
  gap: 0.35rem 1.5rem;
}
.include-list li {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.25rem 0;
  border-bottom: 1px solid var(--border);
}
.include-list strong {
  font-variant-numeric: tabular-nums;
}
.not-included {
  margin: 0 0 0.9rem;
  overflow-wrap: anywhere;
}
.faces-option {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  margin-bottom: 1rem;
}
/* The switch must keep its own width: left to flex it is squeezed to a dot by
   the long label beside it. */
.faces-option :deep(.p-toggleswitch) {
  flex: none;
  margin-top: 0.1rem;
}
.faces-option label {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  cursor: pointer;
}
.backup-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.drive-hint {
  margin: 0.5rem 0 0;
}
.backup-working,
.backup-done,
.backup-error {
  margin-top: 0.8rem;
}
.backup-working :deep(.p-message-text),
.backup-done :deep(.p-message-text),
.backup-error :deep(.p-message-text) {
  color: var(--text);
}
.muted-note {
  color: var(--muted);
  font-size: 0.82rem;
}
</style>
