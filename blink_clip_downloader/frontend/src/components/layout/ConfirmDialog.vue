<script setup lang="ts">
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { useConfirmStore } from '../../stores/confirm'

const confirm = useConfirmStore()

function onHide() {
  if (confirm.open) confirm.settle(false)
}
</script>

<template>
  <Dialog
    :visible="confirm.open"
    modal
    dismissable-mask
    :header="confirm.title"
    :style="{ width: '28rem' }"
    :draggable="false"
    @update:visible="onHide"
  >
    <p class="m-0">{{ confirm.message }}</p>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="confirm.settle(false)" />
      <Button label="Confirm" severity="danger" @click="confirm.settle(true)" />
    </template>
  </Dialog>
</template>
