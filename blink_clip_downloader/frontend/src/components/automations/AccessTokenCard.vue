<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useAccessStore } from '../../stores/access'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

const access = useAccessStore()
const confirm = useConfirmStore()
const toast = useToastStore()

const revealed = ref(false)
const regenerating = ref(false)

async function regenerate() {
  const ok = await confirm.ask(
    'Every camera and script already using the current token stops working until you paste the new YAML into Home Assistant.',
    'Regenerate the access token?',
  )
  if (!ok) return
  regenerating.value = true
  try {
    await access.regenerateToken()
    revealed.value = true
    toast.show('New access token created — copy the YAML below again')
  } catch {
    toast.show('Could not regenerate the access token', true)
  } finally {
    regenerating.value = false
  }
}
</script>

<template>
  <Card v-if="access.loginEnabled" class="access-token-card" data-testid="access-token-card">
    <template #title>🔑 Home Assistant access token</template>
    <template #subtitle>
      The add-on's direct-access port asks for a sign-in. Home Assistant's own calls to it — the Generic Camera snapshot
      URLs, and the Sync now, Arm/Disarm and Archive scripts — carry this token instead. It is already in the YAML the
      builders below generate.
    </template>
    <template #content>
      <div class="access-token-row">
        <label for="access-token" class="access-token-label">Token</label>
        <InputText
          id="access-token"
          :model-value="revealed ? access.accessToken : '•'.repeat(16)"
          readonly
          class="access-token-value"
        />
        <Button
          :label="revealed ? 'Hide' : 'Show'"
          severity="secondary"
          outlined
          size="small"
          data-testid="access-token-toggle"
          @click="revealed = !revealed"
        />
        <Button
          label="Regenerate"
          severity="danger"
          outlined
          size="small"
          :loading="regenerating"
          data-testid="access-token-regenerate"
          @click="regenerate"
        />
      </div>
      <Message severity="secondary" size="small" variant="simple" class="access-token-hint">
        It only opens those few endpoints, never the library or settings. Regenerate it if it has been shared somewhere
        it shouldn't be.
      </Message>
    </template>
  </Card>
</template>

<style scoped>
.access-token-card {
  margin-bottom: 1.5rem;
}
.access-token-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
.access-token-label {
  font-weight: 600;
}
.access-token-value {
  flex: 1 1 min(16rem, 100%);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.access-token-hint {
  margin-top: 0.75rem;
}
</style>
