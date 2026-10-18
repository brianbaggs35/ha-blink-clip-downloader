<script setup lang="ts">
import BackupCreateSection from './BackupCreateSection.vue'
import RestoreSection from './RestoreSection.vue'
</script>

<template>
  <div class="backup-page">
    <h2>Backup &amp; Restore</h2>
    <p class="page-intro">
      Keep a copy of everything this add-on has learned, so reinstalling it — or Home Assistant itself — does not start
      you from scratch. Save the file somewhere other than this device; Google Drive works too, if you have connected
      it.
    </p>

    <section class="backup-section">
      <h3>Back up</h3>
      <BackupCreateSection />
    </section>

    <section class="backup-section">
      <h3>Restore</h3>
      <RestoreSection />
    </section>
  </div>
</template>

<style scoped>
.backup-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  max-width: 900px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — the same fix the Storage and Automations pages
     carry, or a narrow phone gets a page wider than its screen. */
  min-width: 0;
  width: 100%;
}
.page-intro {
  color: var(--muted);
  margin-bottom: 1.5rem;
}
.backup-section {
  margin-bottom: 2rem;
}
.backup-section h3 {
  margin-bottom: 0.75rem;
}
</style>
