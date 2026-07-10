<script setup lang="ts">
import CodeBlock from './CodeBlock.vue'

const NOTIFY_NEW_CLIP = `alias: "Blink – new clip notification"
trigger:
  - platform: event
    event_type: blink_clip_downloaded
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "🎥 New Blink clip – {{ trigger.event.data.camera }}"
      message: >
        {{ trigger.event.data.timestamp[:10] }}
        ({{ (trigger.event.data.size_bytes / 1048576) | round(1) }} MB)`

const LONG_MOTION_CLIP = `alias: "Blink – long motion clip"
trigger:
  - platform: event
    event_type: blink_clip_downloaded
condition:
  - condition: template
    value_template: >
      {{ trigger.event.data.source == 'pir' and
         (trigger.event.data.duration | int(0)) > 10 }}
action:
  - service: notify.notify
    data:
      title: "⚠️ Long motion clip"
      message: "{{ trigger.event.data.camera }} — {{ trigger.event.data.duration }}s"`

const STORAGE_QUOTA_WARNING = `alias: "Blink – storage quota warning"
trigger:
  - platform: numeric_state
    entity_id: sensor.blink_downloader_status
    attribute: used_mb
    above: 8000
action:
  - service: notify.notify
    data:
      title: "💾 Blink storage nearing limit"
      message: >
        {{ state_attr('sensor.blink_downloader_status','used_mb')|int }} MB used.`

const DAILY_SUMMARY = `alias: "Blink – daily summary"
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: notify.notify
    data:
      title: "📅 Blink Daily Summary"
      message: >
        {{ states('sensor.blink_downloader_status') }} total clips.
        {{ state_attr('sensor.blink_downloader_status','session_downloads') }}
        downloaded this session.`
</script>

<template>
  <div class="auto-content">
    <h2>HA Automation Examples</h2>
    <p>
      The add-on fires events and updates a sensor every poll cycle. Copy these snippets into
      <code>automations.yaml</code> or the HA automation editor.
    </p>

    <h3>📡 Events &amp; Sensors</h3>
    <div class="table-scroll">
      <table class="event-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Name</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Sensor</td>
            <td><code>sensor.blink_downloader_status</code></td>
            <td>Total clips; attributes: session_downloads, used_mb, free_gb, last_download</td>
          </tr>
          <tr>
            <td>Event</td>
            <td><code>blink_clip_downloaded</code></td>
            <td>Per-clip event: clip_id, camera, path, timestamp, size_bytes, duration, source</td>
          </tr>
        </tbody>
      </table>
    </div>

    <h3>⚡ Notify on any new clip</h3>
    <CodeBlock :code="NOTIFY_NEW_CLIP" />

    <h3>⚡ Alert on long motion clip (&gt; 10 s)</h3>
    <CodeBlock :code="LONG_MOTION_CLIP" />

    <h3>⚡ Storage quota warning</h3>
    <CodeBlock :code="STORAGE_QUOTA_WARNING" />

    <h3>⚡ Daily summary</h3>
    <CodeBlock :code="DAILY_SUMMARY" />

    <h3>💡 Tips</h3>
    <ul>
      <li>Enable <strong>Watch HA Events</strong> in add-on settings for instant download after motion.</li>
      <li>Tune <strong>Post-Motion Download Delay</strong> (default 30 s) to your Blink upload speed.</li>
      <li>Use <strong>⬇ Sync</strong> in the Library tab to trigger an immediate download cycle.</li>
      <li>Clips default to <code>/share/blink-clips/</code> — separate from HA's <code>/config/snapshots/</code>.</li>
      <li>
        The Video.js player supports keyboard shortcuts: <code>Space</code> play/pause, <code>← →</code> skip 10 s,
        <code>F</code> fullscreen, <code>M</code> mute, <code>↑ ↓</code> prev/next clip.
      </li>
    </ul>
  </div>
</template>
