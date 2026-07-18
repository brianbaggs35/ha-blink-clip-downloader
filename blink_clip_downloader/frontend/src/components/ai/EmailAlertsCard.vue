<script setup lang="ts">
import { ref } from 'vue'
import { testEmail } from '../../api/ai'
import { useToastStore } from '../../stores/toast'

defineProps<{ smtpConfigured: boolean }>()

const toast = useToastStore()
const sending = ref(false)
const result = ref<{ success: boolean; message: string } | null>(null)

async function sendTestEmail() {
  sending.value = true
  result.value = null
  try {
    const r = await testEmail()
    result.value = r
    toast.show(r.success ? 'Test email sent' : 'Test email failed', !r.success)
  } catch {
    result.value = { success: false, message: 'Failed to send test email — check the add-on logs' }
    toast.show('Test email failed', true)
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">Email Alerts</h3>
    <div v-if="!smtpConfigured" style="font-size: 0.82rem; color: var(--muted)">
      No SMTP settings configured — set the SMTP Host and Email Alert Recipients options in the add-on's
      <strong>Configuration</strong> tab to enable email alerts.
    </div>
    <div v-else>
      <button class="btn sm ghost" :disabled="sending" @click="sendTestEmail">
        {{ sending ? '⏳ Sending…' : '✉️ Send Test Email' }}
      </button>
      <p style="font-size: 0.73rem; color: var(--muted); margin: 0.3rem 0 0">
        Sends a one-off test email to verify SMTP settings, even if Enable Email Alerts is currently off.
      </p>
      <div
        v-if="result"
        style="margin-top: 0.45rem; font-size: 0.8rem"
        :style="{ color: result.success ? 'var(--success)' : 'var(--danger)' }"
      >
        {{ result.success ? '✓ ' : '✗ ' }}{{ result.message }}
      </div>
    </div>
  </div>
</template>
