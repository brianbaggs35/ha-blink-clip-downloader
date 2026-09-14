<script setup lang="ts">
import { ref, watch } from 'vue'
import Button from 'primevue/button'
import { getVehicleSignature, resetVehicleSignature } from '../../api/security'
import type { VehicleSignatureInfo } from '../../api/types'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ camera: string }>()

const confirm = useConfirmStore()
const toast = useToastStore()

const info = ref<VehicleSignatureInfo | null>(null)
const resetting = ref(false)

async function load() {
  try {
    info.value = await getVehicleSignature(props.camera)
  } catch {
    info.value = null
  }
}

watch(() => props.camera, load, { immediate: true })

/** Resetting is the escape hatch for the one way this feature can go wrong:
 *  a signature that has latched onto the neighbour's car will keep
 *  "confirming" its own mistake every time it matches. */
async function reset() {
  const ok = await confirm.ask(
    `Forget what ${props.camera} has learned about the protected vehicle? ` +
      'It will start learning again from the next analyzed clip.',
  )
  if (!ok) return
  resetting.value = true
  try {
    await resetVehicleSignature(props.camera)
    toast.show('Learned vehicle position cleared')
    await load()
  } catch {
    toast.show('Failed to clear the learned vehicle position', true)
  } finally {
    resetting.value = false
  }
}
</script>

<template>
  <div v-if="info" class="signature-row" data-testid="vehicle-signature">
    <span v-if="!info.learned" class="signature-note">
      Still learning where your vehicle normally sits on this camera. Until then, identification relies on the zone
      above.
    </span>
    <span v-else-if="!info.established" class="signature-note">
      Learning where your vehicle sits ({{ info.sample_count }} of 4 confident sightings so far).
    </span>
    <span v-else class="signature-note">
      Learned where your vehicle sits from {{ info.sample_count }} confident sightings — used to tell it apart from
      other vehicles in frame.
    </span>
    <Button
      v-if="info.learned"
      label="Reset"
      size="small"
      severity="secondary"
      text
      :loading="resetting"
      @click="reset"
    />
  </div>
</template>

<style scoped>
.signature-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-top: 0.6rem;
}
.signature-note {
  flex: 1 1 min(280px, 100%);
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
