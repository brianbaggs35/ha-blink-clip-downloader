<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getActivity, getCameras, getStats } from '../../api/clips'
import { getAiStatus } from '../../api/ai'
import { getBatteryStatus } from '../../api/battery'
import { fmtTs, providerLabel } from '../../api/constants'
import type { ActivityRow, AiStatus, BatteryStatus, CameraStat, LibraryStats } from '../../api/types'
import ActivityChart from './ActivityChart.vue'
import BatteryHistoryModal from './BatteryHistoryModal.vue'
import BatteryStatusStrip from './BatteryStatusStrip.vue'
import { useConnectionStore } from '../../stores/connection'
import { useDateFilterStore } from '../../stores/dateFilter'
import { useRefreshStore } from '../../stores/refresh'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

const loading = ref(true)
const error = ref(false)
const stats = ref<LibraryStats | null>(null)
const cameras = ref<CameraStat[]>([])
const activity = ref<ActivityRow[]>([])
const aiStatus = ref<AiStatus | null>(null)
const batteryStatus = ref<BatteryStatus[]>([])
const selectedBatteryCamera = ref<string | null>(null)

const connection = useConnectionStore()
const dateFilter = useDateFilterStore()
const refresh = useRefreshStore()

const diskPct = computed(() => {
  const disk = stats.value?.disk
  if (!disk?.quota_bytes) return null
  return Math.min(100, (disk.used_bytes / disk.quota_bytes) * 100)
})
const diskClass = computed(() => {
  const pct = diskPct.value
  if (pct == null) return 'ok'
  if (pct > 90) return 'danger'
  if (pct > 70) return 'warn'
  return 'ok'
})
const frameStats = computed(() => aiStatus.value?.analysis_stats)

async function load() {
  loading.value = true
  error.value = false
  try {
    const [statsRes, camsRes, actRes, aiRes, batteryRes] = await Promise.all([
      getStats(),
      getCameras(),
      getActivity(7),
      getAiStatus().catch(() => null),
      getBatteryStatus(),
    ])
    stats.value = statsRes
    cameras.value = camsRes
    activity.value = actRes
    aiStatus.value = aiRes
    batteryStatus.value = batteryRes
    if (typeof statsRes.connected === 'boolean') connection.setConnected(statsRes.connected)
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

function onSelectDate(date: string) {
  dateFilter.requestDate(date)
}

onMounted(load)
watch(() => refresh.tick, load)
</script>

<template>
  <div v-if="loading" style="padding: 2rem; width: 100%"><LoadingIndicator /></div>
  <div v-else-if="error" style="padding: 2rem; width: 100%; color: var(--danger)">Failed to load status.</div>
  <div v-else id="status-page-content">
    <BatteryStatusStrip
      v-if="batteryStatus.length"
      :readings="batteryStatus"
      @select-camera="selectedBatteryCamera = $event"
    />
    <div id="status-grid" class="status-grid">
      <div class="status-card">
        <h3>📡 Blink Connection</h3>
        <div class="status-row">
          <span class="lbl">Status</span>
          <span class="val" :class="stats?.connected ? 'ok' : 'err'">{{
            stats?.connected ? 'Connected' : 'Disconnected'
          }}</span>
        </div>
        <div v-if="stats?.account_id" class="status-row">
          <span class="lbl">Account ID</span>
          <span class="val">{{ stats.account_id }}</span>
        </div>
        <div v-if="stats?.last_download" class="status-row">
          <span class="lbl">Last download</span>
          <span class="val wrap">{{ fmtTs(stats.last_download) }}</span>
        </div>
      </div>

      <div class="status-card">
        <h3>📚 Clip Library</h3>
        <div class="status-row">
          <span class="lbl">Total clips</span>
          <span class="val">{{ stats?.total_count ?? 0 }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Today</span>
          <span class="val">{{ stats?.today_count ?? 0 }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">This week</span>
          <span class="val">{{ stats?.week_count ?? 0 }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Starred</span>
          <span class="val">{{ stats?.starred_count ?? 0 }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Archived</span>
          <span class="val">{{ stats?.archived_count ?? 0 }}</span>
        </div>
      </div>

      <div v-if="stats?.disk" class="status-card">
        <h3>💾 Storage</h3>
        <div class="status-row">
          <span class="lbl">Used</span>
          <span class="val" :class="diskClass">{{ stats.disk.used_mb }} MB</span>
        </div>
        <div class="status-row">
          <span class="lbl">Free (disk)</span>
          <span class="val">{{ stats.disk.free_gb }} GB</span>
        </div>
        <template v-if="stats.disk.quota_bytes">
          <div class="status-row">
            <span class="lbl">Quota</span>
            <span class="val">{{ stats.disk.quota_gb }} GB</span>
          </div>
          <div class="prog-bar">
            <div class="prog-fill" :class="diskClass" :style="{ width: `${(diskPct || 0).toFixed(1)}%` }"></div>
          </div>
        </template>
      </div>

      <div v-if="frameStats?.total_frames_analyzed" class="status-card">
        <h3>🖼️ Frames Analyzed</h3>
        <div class="status-row">
          <span class="lbl">Total frames</span>
          <span class="val">{{ frameStats.total_frames_analyzed }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Today</span>
          <span class="val">{{ frameStats.frames_analyzed_today || 0 }}</span>
        </div>
      </div>

      <div v-if="cameras.length" class="status-card">
        <h3>📷 Cameras ({{ cameras.length }})</h3>
        <div v-for="cam in cameras" :key="cam.camera" class="status-row">
          <span class="lbl">{{ cam.camera }}</span>
          <span class="val">{{ cam.total || 0 }} clips — {{ cam.today || 0 }} today</span>
        </div>
      </div>

      <div v-if="aiStatus?.enabled" class="status-card">
        <h3>🤖 AI Analysis</h3>
        <div class="status-row">
          <span class="lbl">Status</span>
          <span class="val" :class="aiStatus.ai_online ? 'ok' : 'err'">{{
            aiStatus.ai_online ? 'Online' : 'Offline'
          }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Provider</span>
          <span class="val">{{ providerLabel(aiStatus.provider) }}</span>
        </div>
        <div class="status-row">
          <span class="lbl">Model</span>
          <span class="val">{{ aiStatus.model || '—' }}</span>
        </div>
        <div v-if="aiStatus.queue?.pending !== undefined" class="status-row">
          <span class="lbl">Pending</span>
          <span class="val">{{ aiStatus.queue.pending || 0 }}</span>
        </div>
        <div v-if="frameStats?.total_analyzed" class="status-row">
          <span class="lbl">Analyzed</span>
          <span class="val">{{ frameStats.total_analyzed }}</span>
        </div>
        <div v-if="frameStats?.suspicious_count" class="status-row">
          <span class="lbl">Suspicious</span>
          <span class="val" style="color: var(--danger)">{{ frameStats.suspicious_count }}</span>
        </div>
      </div>

      <div class="status-card" style="grid-column: 1 / -1">
        <h3>📅 Activity — last 7 days</h3>
        <ActivityChart :rows="activity" @select-date="onSelectDate" />
      </div>
    </div>
  </div>
  <BatteryHistoryModal
    v-if="selectedBatteryCamera"
    :camera="selectedBatteryCamera"
    @close="selectedBatteryCamera = null"
  />
</template>
