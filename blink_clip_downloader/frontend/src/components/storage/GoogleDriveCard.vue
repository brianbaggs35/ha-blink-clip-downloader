<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import ProgressBar from 'primevue/progressbar'
import Select from 'primevue/select'
import {
  disconnectGDrive,
  getGDriveConnectStatus,
  getGDriveQuota,
  getGDriveQueueStatus,
  getGDriveSettings,
  getGDriveStatus,
  saveGDriveSettings,
  selectGDriveFolder,
  startGDriveConnect,
  triggerGDriveBackupNow,
} from '../../api/gdrive'
import type {
  GDriveBackupPolicy,
  GDriveConnectState,
  GDriveQueueStatus,
  GDriveQuota,
  GDriveSettings,
  GDriveStatus,
} from '../../api/types'
import { fmtSize } from '../../api/constants'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import GoogleDriveFolderBrowser from './GoogleDriveFolderBrowser.vue'

const BACKUP_POLICY_OPTIONS: { label: string; value: GDriveBackupPolicy }[] = [
  { label: 'Archived clips only (recommended)', value: 'archived_only' },
  { label: 'All clips (archived + regular downloads)', value: 'all_clips' },
]

const CONNECT_POLL_INTERVAL_MS = 3000

const toast = useToastStore()
const confirm = useConfirm()

const loading = ref(true)
const settings = ref<GDriveSettings>({ client_id: '', has_client_secret: false, backup_policy: 'archived_only' })
const status = ref<GDriveStatus>({
  configured: false,
  connected: false,
  account_email: '',
  folder_id: '',
  folder_name: '',
})
const quota = ref<GDriveQuota | null>(null)
const queueStatus = ref<GDriveQueueStatus | null>(null)
const connectState = ref<GDriveConnectState>({ phase: 'idle' })

const clientIdInput = ref('')
const clientSecretInput = ref('')
const backupPolicyInput = ref<GDriveBackupPolicy>('archived_only')
const savingSettings = ref(false)
const connecting = ref(false)
const backingUpNow = ref(false)
const showFolderDialog = ref(false)

let connectPollTimer: ReturnType<typeof setInterval> | undefined

const isConfigured = computed(() => settings.value.client_id !== '' && settings.value.has_client_secret)

const quotaPercent = computed(() => {
  if (!quota.value?.available || !quota.value.limit) return null
  return Math.min(100, Math.round(((quota.value.usage || 0) / quota.value.limit) * 100))
})

async function loadSettings() {
  settings.value = await getGDriveSettings()
  clientIdInput.value = settings.value.client_id
  backupPolicyInput.value = settings.value.backup_policy
}

async function loadStatus() {
  status.value = await getGDriveStatus()
}

async function loadQuota() {
  if (!status.value.connected) {
    quota.value = null
    return
  }
  try {
    quota.value = await getGDriveQuota()
  } catch {
    quota.value = null
  }
}

async function loadQueueStatus() {
  try {
    queueStatus.value = await getGDriveQueueStatus()
  } catch {
    queueStatus.value = null
  }
}

async function loadAll() {
  loading.value = true
  try {
    await loadSettings()
    await loadStatus()
    await Promise.all([loadQuota(), loadQueueStatus()])
  } catch {
    toast.show('Failed to load Google Drive status', true)
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  savingSettings.value = true
  try {
    await saveGDriveSettings(
      clientIdInput.value.trim(),
      clientSecretInput.value.trim() || null,
      backupPolicyInput.value,
    )
    clientSecretInput.value = ''
    toast.show('Google Drive settings saved')
    await loadSettings()
  } catch {
    toast.show('Could not save Google Drive settings', true)
  } finally {
    savingSettings.value = false
  }
}

function applyConnectState(state: GDriveConnectState) {
  connectState.value = state
}

function stopConnectPolling() {
  if (connectPollTimer) clearInterval(connectPollTimer)
  connectPollTimer = undefined
}

function startConnectPolling() {
  stopConnectPolling()
  connectPollTimer = setInterval(async () => {
    try {
      const state = await getGDriveConnectStatus()
      applyConnectState(state)
      if (state.phase !== 'pending') {
        stopConnectPolling()
        if (state.phase === 'connected') {
          toast.show('Connected to Google Drive')
          void loadStatus()
        }
      }
    } catch {
      // Transient — keep polling rather than giving up on one dropped request.
    }
  }, CONNECT_POLL_INTERVAL_MS)
}

async function connect() {
  connecting.value = true
  try {
    const state = await startGDriveConnect()
    applyConnectState(state)
    if (state.phase === 'pending') startConnectPolling()
  } catch {
    toast.show('Could not start Google Drive sign-in', true)
  } finally {
    connecting.value = false
  }
}

async function disconnect() {
  if (!(await confirm('Disconnect Google Drive? Automatic backups will stop until you reconnect.'))) return
  try {
    await disconnectGDrive()
    toast.show('Disconnected from Google Drive')
    connectState.value = { phase: 'idle' }
    await loadStatus()
  } catch {
    toast.show('Could not disconnect', true)
  }
}

async function onFolderSelected(folder: { id: string; name: string }) {
  try {
    await selectGDriveFolder(folder.id, folder.name)
    showFolderDialog.value = false
    toast.show(`Backup folder set to "${folder.name}"`)
    await loadStatus()
  } catch {
    toast.show('Could not select that folder', true)
  }
}

async function backupNow() {
  backingUpNow.value = true
  try {
    const res = await triggerGDriveBackupNow()
    toast.show(`Queued ${res.enqueued} clip(s) for backup`)
    void loadQueueStatus()
  } catch {
    toast.show('Could not start backup', true)
  } finally {
    backingUpNow.value = false
  }
}

onMounted(async () => {
  await loadAll()
  if (status.value.connected) return
  try {
    const state = await getGDriveConnectStatus()
    if (state.phase === 'pending') {
      applyConnectState(state)
      startConnectPolling()
    }
  } catch {
    // No in-flight connect — nothing to resume.
  }
})
onUnmounted(stopConnectPolling)
</script>

<template>
  <div class="gdrive-cards">
    <Card class="gdrive-setup-card">
      <template #title>
        <span class="gdrive-title">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" class="gdrive-mark">
            <path
              fill="#4285F4"
              d="M12.01 1.485c-2.082 0-3.754.02-3.743.047.01.02 1.708 3.001 3.774 6.62l3.76 6.574h3.76c2.081 0 3.753-.02 3.742-.047-.005-.02-1.708-3.001-3.775-6.62l-3.76-6.574zm-4.76 1.73a789.828 789.861 0 0 0-3.63 6.319L0 15.868l1.89 3.298 1.885 3.297 3.62-6.335 3.618-6.33-1.88-3.287C8.1 4.704 7.255 3.22 7.25 3.214zm2.259 12.653-.203.348c-.114.198-.96 1.672-1.88 3.287a423.93 423.948 0 0 1-1.698 2.97c-.01.026 3.24.042 7.222.042h7.244l1.796-3.157c.992-1.734 1.85-3.23 1.906-3.323l.104-.167h-7.249z"
            />
          </svg>
          Google Drive Setup
        </span>
      </template>
      <template #subtitle>
        Requires a one-time OAuth client you register yourself in Google Cloud Console (type "TVs and Limited Input
        devices") — see the docs for exact steps. Everything after that happens right here.
      </template>
      <template #content>
        <div v-if="loading" class="gdrive-status"><LoadingIndicator /></div>
        <template v-else>
          <label class="field-label" for="gdrive-client-id">OAuth Client ID</label>
          <InputText
            id="gdrive-client-id"
            v-model="clientIdInput"
            class="gdrive-input"
            placeholder="xxxx.apps.googleusercontent.com"
          />
          <label class="field-label" for="gdrive-client-secret">OAuth Client Secret</label>
          <Password
            id="gdrive-client-secret"
            v-model="clientSecretInput"
            class="gdrive-input"
            :feedback="false"
            toggle-mask
            :placeholder="settings.has_client_secret ? 'Already set — leave blank to keep it' : 'Client secret'"
          />
          <label class="field-label" for="gdrive-backup-policy">Backup Policy</label>
          <Select
            id="gdrive-backup-policy"
            v-model="backupPolicyInput"
            class="gdrive-input"
            :options="BACKUP_POLICY_OPTIONS"
            option-label="label"
            option-value="value"
          />
          <Button
            size="small"
            :disabled="savingSettings"
            :loading="savingSettings"
            label="Save Setup"
            @click="saveSettings"
          />
        </template>
      </template>
    </Card>

    <Card class="gdrive-connection-card">
      <template #title>Connection</template>
      <template #content>
        <div v-if="loading" class="gdrive-status"><LoadingIndicator /></div>
        <Message v-else-if="!isConfigured" severity="info" :closable="false">
          Set a Google OAuth Client ID and Secret above to connect an account.
        </Message>
        <template v-else-if="connectState.phase === 'pending'">
          <p>
            Go to
            <a :href="connectState.verification_url" target="_blank" rel="noopener">{{
              connectState.verification_url
            }}</a>
            and enter this code:
          </p>
          <p class="gdrive-user-code">{{ connectState.user_code }}</p>
          <p class="muted-note">Waiting for confirmation…</p>
        </template>
        <template v-else-if="!status.connected">
          <Message v-if="connectState.phase === 'error'" severity="error" :closable="false">
            {{ connectState.message || 'Sign-in failed.' }}
          </Message>
          <Message v-else-if="connectState.phase === 'expired'" severity="warn" :closable="false">
            Sign-in code expired — try again.
          </Message>
          <Button
            size="small"
            :disabled="connecting"
            :loading="connecting"
            label="Connect Google Drive"
            @click="connect"
          />
        </template>
        <template v-else-if="!status.folder_id">
          <p class="muted-note">Connected as {{ status.account_email }}. Choose a folder for your backups:</p>
          <GoogleDriveFolderBrowser @select="onFolderSelected" />
        </template>
        <template v-else>
          <div class="gdrive-connected-row">
            <span
              >Connected as <strong>{{ status.account_email }}</strong></span
            >
            <Button size="small" severity="secondary" outlined label="Disconnect" @click="disconnect" />
          </div>
          <div class="gdrive-connected-row">
            <span
              >Backup folder: <strong>{{ status.folder_name }}</strong></span
            >
            <Button size="small" severity="secondary" outlined label="Change Folder" @click="showFolderDialog = true" />
          </div>

          <template v-if="quota?.available">
            <p class="field-label">Drive Storage</p>
            <ProgressBar v-if="quotaPercent !== null" :value="quotaPercent" />
            <p class="muted-note">
              {{
                quotaPercent === null
                  ? 'Unlimited storage'
                  : `${fmtSize(quota.usage)} of ${fmtSize(quota.limit ?? 0)} used`
              }}
            </p>
          </template>

          <p v-if="queueStatus" class="muted-note">
            Backup queue: {{ queueStatus.pending }} pending, {{ queueStatus.processing }} uploading,
            {{ queueStatus.completed }} completed, {{ queueStatus.failed }} failed.
          </p>

          <Button
            size="small"
            :disabled="backingUpNow"
            :loading="backingUpNow"
            label="Back Up Existing Clips Now"
            @click="backupNow"
          />
        </template>
      </template>
    </Card>

    <Dialog
      v-model:visible="showFolderDialog"
      modal
      dismissable-mask
      header="Change Backup Folder"
      :style="{ width: '38rem' }"
      :draggable="false"
    >
      <GoogleDriveFolderBrowser @select="onFolderSelected" />
    </Dialog>
  </div>
</template>

<style scoped>
.gdrive-cards {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.gdrive-title {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}

.gdrive-mark {
  flex-shrink: 0;
}

.gdrive-status {
  padding: 1rem;
}

.field-label {
  display: block;
  font-size: 0.82rem;
  color: var(--muted);
  margin: 0.6rem 0 0.3rem;
}

.gdrive-input {
  width: 100%;
  margin-bottom: 0.4rem;
}

.gdrive-user-code {
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: 0.15em;
  margin: 0.5rem 0;
}

.gdrive-connected-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
  flex-wrap: wrap;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
