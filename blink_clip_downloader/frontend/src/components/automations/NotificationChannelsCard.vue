<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import { testDiscord, testEmail, testMobile } from '../../api/ai'
import { useToastStore } from '../../stores/toast'

type ChannelResult = { success: boolean; message: string } | null

const toast = useToastStore()

const sendingEmail = ref(false)
const sendingDiscord = ref(false)
const sendingMobile = ref(false)
const emailResult = ref<ChannelResult>(null)
const discordResult = ref<ChannelResult>(null)
const mobileResult = ref<ChannelResult>(null)

async function runTest(
  label: string,
  sending: typeof sendingEmail,
  result: typeof emailResult,
  fn: () => Promise<{ success: boolean; message: string }>,
) {
  sending.value = true
  result.value = null
  try {
    const r = await fn()
    result.value = r
    toast.show(r.success ? `${label} sent` : `${label} failed`, !r.success)
  } catch {
    result.value = { success: false, message: `Failed to send — check the add-on logs` }
    toast.show(`${label} failed`, true)
  } finally {
    sending.value = false
  }
}

const sendEmail = () => runTest('Test email', sendingEmail, emailResult, testEmail)
const sendDiscord = () => runTest('Test Discord message', sendingDiscord, discordResult, testDiscord)
const sendMobile = () => runTest('Test push notification', sendingMobile, mobileResult, testMobile)
</script>

<template>
  <Card class="notification-channels-card">
    <template #title>🔔 Notification Channels</template>
    <template #subtitle>
      Send a one-off test through each channel to verify it's configured correctly — even if the channel is currently
      disabled, so you can check credentials before flipping it on.
    </template>
    <template #content>
      <div class="channel-row">
        <div class="channel-row-main">
          <strong>Email</strong>
          <span class="channel-row-hint">SMTP settings in the add-on's Configuration tab</span>
        </div>
        <Button size="small" outlined :disabled="sendingEmail" :loading="sendingEmail" @click="sendEmail">
          {{ sendingEmail ? 'Sending…' : 'Send test email' }}
        </Button>
      </div>
      <Message
        v-if="emailResult"
        class="channel-result"
        :severity="emailResult.success ? 'success' : 'error'"
        :closable="false"
        size="small"
      >
        {{ emailResult.message }}
      </Message>

      <div class="channel-row">
        <div class="channel-row-main">
          <strong>Discord</strong>
          <span class="channel-row-hint">Webhook URL in the add-on's Configuration tab</span>
        </div>
        <Button size="small" outlined :disabled="sendingDiscord" :loading="sendingDiscord" @click="sendDiscord">
          {{ sendingDiscord ? 'Sending…' : 'Send test message' }}
        </Button>
      </div>
      <Message
        v-if="discordResult"
        class="channel-result"
        :severity="discordResult.success ? 'success' : 'error'"
        :closable="false"
        size="small"
      >
        {{ discordResult.message }}
      </Message>

      <div class="channel-row">
        <div class="channel-row-main">
          <strong>Mobile App</strong>
          <span class="channel-row-hint">HA Companion app target in the add-on's Configuration tab</span>
        </div>
        <Button size="small" outlined :disabled="sendingMobile" :loading="sendingMobile" @click="sendMobile">
          {{ sendingMobile ? 'Sending…' : 'Send test notification' }}
        </Button>
      </div>
      <Message
        v-if="mobileResult"
        class="channel-result"
        :severity="mobileResult.success ? 'success' : 'error'"
        :closable="false"
        size="small"
      >
        {{ mobileResult.message }}
      </Message>
    </template>
  </Card>
</template>

<style scoped>
.notification-channels-card {
  margin-bottom: 1.5rem;
}

.channel-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.6rem 0;
  border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}

.channel-row:last-of-type {
  border-bottom: none;
}

.channel-result {
  margin: 0.6rem 0;
}

.channel-row-main {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.channel-row-hint {
  font-size: 0.76rem;
  color: var(--muted);
}
</style>
