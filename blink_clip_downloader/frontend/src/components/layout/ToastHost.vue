<script setup lang="ts">
import { useToastStore } from '../../stores/toast'

const toast = useToastStore()
</script>

<template>
  <div class="toast" :class="{ show: toast.visible, err: toast.isError }">{{ toast.message }}</div>
</template>
