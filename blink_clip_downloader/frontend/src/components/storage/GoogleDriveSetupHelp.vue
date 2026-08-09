<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

const CONSOLE_URL = 'https://console.cloud.google.com/'

const visible = ref(false)
</script>

<template>
  <Button
    text
    rounded
    size="small"
    severity="secondary"
    icon="pi pi-info-circle"
    title="How to connect Google Drive"
    aria-label="How to connect Google Drive"
    @click="visible = true"
  />

  <Dialog
    v-model:visible="visible"
    modal
    dismissable-mask
    header="Connecting Google Drive"
    :style="{ width: '34rem' }"
    class="gdrive-help-dialog"
    :draggable="false"
  >
    <p>
      Google requires every app talking to its APIs to be registered as its own OAuth client — this add-on can't ship
      one shared client for every install, so you register a free one yourself. It only takes a few minutes and is only
      needed once.
    </p>

    <ol class="gdrive-help-steps">
      <li>
        Go to
        <a :href="CONSOLE_URL" target="_blank" rel="noopener">Google Cloud Console</a>
        and create a new project (or reuse an existing one).
      </li>
      <li>
        Under <strong>APIs &amp; Services → Library</strong>, search for and enable the
        <strong>Google Drive API</strong>.
      </li>
      <li>
        Under <strong>APIs &amp; Services → OAuth consent screen</strong>, configure it — choosing
        <strong>External</strong>
        is fine for personal use — and add yourself as a test user.
      </li>
      <li>
        Under <strong>APIs &amp; Services → Credentials → Create Credentials → OAuth client ID</strong>, set the
        application type to <strong>"TVs and Limited Input devices"</strong>. This is important: it's the only client
        type that supports the code-based sign-in this add-on uses, and it's why no redirect URL needs configuring.
      </li>
      <li>Copy the generated <strong>Client ID</strong> and <strong>Client Secret</strong>.</li>
      <li>
        Paste both into the <strong>Google Drive Setup</strong> card behind this dialog, choose a backup policy, and
        click <strong>Save Setup</strong>.
      </li>
      <li>
        Click <strong>Connect Google Drive</strong>. You'll get a short code and a URL — open it on any device, sign in,
        and enter the code. This page updates automatically once you're done.
      </li>
    </ol>

    <p class="gdrive-help-note">
      <strong>One more thing:</strong> once you've confirmed it works, move the app from "Testing" to "In production" on
      the <strong>OAuth consent screen</strong> page — Google commonly expires refresh tokens after about a week for
      apps left in Testing, which would otherwise force you to reconnect weekly. The narrow permission this add-on
      requests (access only to files/folders it creates itself, nothing else in your Drive) doesn't require Google's
      full verification review to do this.
    </p>
  </Dialog>
</template>

<style scoped>
.gdrive-help-steps {
  margin: 0 0 1rem;
  padding-left: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.gdrive-help-note {
  color: var(--muted);
  font-size: 0.85rem;
  margin-bottom: 0;
}
</style>
