<script setup lang="ts">
/** What the add-on actually publishes to Home Assistant.
 *
 * Every row here is something ha_entities.py or app.py really writes — the
 * builders on the other tabs generate YAML against exactly these names, so
 * this table doubles as the reference for anyone writing their own.
 */
const SENSORS = [
  {
    entity: 'sensor.blink_downloader_status',
    state: 'Clips downloaded, in total',
    attributes: 'session_downloads, used_mb, free_gb, last_download, total_downloaded',
  },
  {
    entity: 'sensor.blink_local_storage',
    state: 'Percent of the clip library quota in use (of the disk, when no quota is set)',
    attributes: 'basis, percent_used, clips_used_gb, clips_used_mb, quota_gb, disk_free_gb, disk_total_gb',
  },
  {
    entity: 'sensor.blink_cloud_storage',
    state: 'Percent of the cloud backup account in use — unknown when it is not connected or is unlimited',
    attributes:
      'provider, configured, connected, unlimited, percent_used, used_gb, drive_files_gb, total_gb, free_gb, pending_uploads, failed_uploads, uploaded_clips, uploads_paused, pause_reason',
  },
]

const EVENTS = [
  {
    event: 'blink_clip_downloaded',
    when: 'Each clip, as it lands on disk',
    data: 'clip_id, camera, path, timestamp, size_bytes, duration, source',
  },
  {
    event: 'blink_clip_analyzed',
    when: 'Each finished AI analysis, suspicious or not',
    data: 'clip_id, camera, is_suspicious, confidence, summary, risk_score, severity, event_type, evidence_quality, face_recognized, model, path',
  },
  {
    event: 'blink_camera_battery_low',
    when: 'A camera transitions to low battery — once per transition, not per poll',
    data: 'camera, battery_state, battery_level, battery_voltage',
  },
]
</script>

<template>
  <div class="entity-ref">
    <h4 class="ref-head">Sensors</h4>
    <div class="table-scroll">
      <table class="event-table">
        <thead>
          <tr>
            <th>Entity</th>
            <th>State</th>
            <th>Attributes</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in SENSORS" :key="row.entity">
            <td>
              <code>{{ row.entity }}</code>
            </td>
            <td>{{ row.state }}</td>
            <td class="ref-attrs">{{ row.attributes }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <h4 class="ref-head">Events</h4>
    <div class="table-scroll">
      <table class="event-table">
        <thead>
          <tr>
            <th>Event</th>
            <th>Fired</th>
            <th>Data</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in EVENTS" :key="row.event">
            <td>
              <code>{{ row.event }}</code>
            </td>
            <td>{{ row.when }}</td>
            <td class="ref-attrs">{{ row.data }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="ref-note">
      The two storage sensors are written once per poll cycle and once at startup, so a threshold automation is current
      within a poll interval of a restart. Nothing here needs the AI features turned on except
      <code>blink_clip_analyzed</code>.
    </p>
  </div>
</template>

<style scoped>
.ref-head {
  font-size: 0.85rem;
  font-weight: 700;
  color: var(--text);
  margin: 0.2rem 0 0.4rem;
}
.ref-attrs {
  font-size: 0.74rem;
  color: var(--muted);
}
.ref-note {
  font-size: 0.79rem;
  color: var(--muted);
  line-height: 1.55;
  margin: 0.8rem 0 0;
}
</style>
