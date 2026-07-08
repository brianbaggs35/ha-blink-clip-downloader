<script setup lang="ts">
import { useConfirmStore } from '../../stores/confirm'

const confirm = useConfirmStore()
</script>

<template>
  <div
    class="modal-bg"
    :class="{ open: confirm.open }"
    style="z-index: 200"
    @click.self="confirm.settle(false)"
  >
    <div class="modal" style="max-width: 420px">
      <div class="modal-body" style="padding: 1.6rem 1.5rem">
        <div class="modal-title" style="font-size: 1.05rem; margin-bottom: 0.5rem">
          {{ confirm.title }}
        </div>
        <p
          style="color: var(--muted); font-size: 0.86rem; line-height: 1.55; margin-bottom: 1.3rem"
        >
          {{ confirm.message }}
        </p>
        <div style="display: flex; gap: 0.6rem; justify-content: flex-end">
          <button class="btn sm ghost" @click="confirm.settle(false)">Cancel</button>
          <button class="btn sm danger" @click="confirm.settle(true)">Confirm</button>
        </div>
      </div>
    </div>
  </div>
</template>
