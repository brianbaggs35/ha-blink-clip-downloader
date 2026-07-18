<script setup lang="ts">
import ArchivedClipsSection from './ArchivedClipsSection.vue'
import GoogleDriveCard from './GoogleDriveCard.vue'
</script>

<template>
  <div class="storage-page">
    <h2>Storage</h2>
    <p class="page-intro">
      Manage clips that have been compressed into ZIP archives, and back them up to Google Drive as an extra copy.
    </p>

    <section class="storage-section">
      <h3>Archived Clips</h3>
      <ArchivedClipsSection />
    </section>

    <section class="storage-section">
      <h3>Google Drive Backup</h3>
      <GoogleDriveCard />
    </section>

    <p class="gdrive-disclaimer">
      Google Drive is a trademark of Google LLC. This add-on is not affiliated with, endorsed by, or sponsored by
      Google.
    </p>
  </div>
</template>

<style scoped>
.storage-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  max-width: 900px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — on a narrow (mobile) viewport that pushed
     this whole page wider than the screen instead of wrapping its text,
     the same fix .auto-content (Automations/AI/Models/Vehicles) already has. */
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1.5rem;
}

.storage-section {
  margin-bottom: 2rem;
}

.storage-section h3 {
  margin-bottom: 0.75rem;
}

.gdrive-disclaimer {
  color: var(--muted);
  font-size: 0.75rem;
  text-align: center;
  margin-top: 2rem;
}
</style>
