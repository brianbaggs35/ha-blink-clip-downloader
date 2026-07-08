<script setup lang="ts">
import AppIcon from '../icons/AppIcon.vue'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()
</script>

<template>
  <div class="auth-error-banner" :class="{ show: auth.bannerVisible }">
    <AppIcon name="error-circle" />
    <span>{{ auth.bannerMessage }}</span>
    <button title="Dismiss" @click="auth.dismissBanner()"><AppIcon name="close" /></button>
  </div>
</template>
