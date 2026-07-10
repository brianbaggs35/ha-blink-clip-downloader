<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { deleteFace, enrollFace, listFaces } from '../../api/ai'
import type { FaceEnrollment } from '../../api/types'
import { useToastStore } from '../../stores/toast'

const toast = useToastStore()

const available = ref(true)
const faces = ref<FaceEnrollment[]>([])
const name = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const enrolling = ref(false)

async function load() {
  try {
    const data = await listFaces()
    available.value = data.available
    faces.value = data.faces
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.error-only handling */
  }
}
onMounted(load)

async function remove(id: number) {
  try {
    await deleteFace(id)
    await load()
  } catch {
    toast.show('Failed to delete enrollment', true)
  }
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

async function enroll() {
  const trimmedName = name.value.trim()
  const file = fileInput.value?.files?.[0]
  if (!trimmedName) {
    toast.show('Enter a name', true)
    return
  }
  if (!file) {
    toast.show('Choose a photo', true)
    return
  }
  enrolling.value = true
  try {
    const imageBase64 = await fileToBase64(file)
    await enrollFace(trimmedName, imageBase64)
    name.value = ''
    if (fileInput.value) fileInput.value.value = ''
    toast.show(`Enrolled ${trimmedName}`)
    await load()
  } catch (e) {
    toast.show(`Enrollment failed: ${e instanceof Error ? e.message : 'unknown error'}`, true)
  } finally {
    enrolling.value = false
  }
}
</script>

<template>
  <div style="margin-bottom: 1.5rem">
    <h3 style="margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem">
      🙂 Face Recognition Enrollment
      <span style="font-size: 0.73rem; color: var(--muted); font-weight: 400">
        — Local only, never uploaded to any AI provider
      </span>
    </h3>
    <p style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.65rem">
      Requires <strong>Enable Local Face Recognition</strong> in the add-on's Configuration tab. Enroll household
      members here so their clips can be recognized and treated as routine — enrollment photos are converted to a
      numeric embedding and stored in this add-on's own database; the photo itself is not kept.
    </p>
    <div
      v-if="!available"
      style="
        background: rgba(245, 158, 11, 0.12);
        border: 1px solid var(--warn, #f59e0b);
        border-radius: 0.4rem;
        padding: 0.6rem 0.8rem;
        font-size: 0.78rem;
        margin-bottom: 0.65rem;
      "
    >
      ⚠️ Face-recognition dependencies are not installed in this image.
    </div>
    <div style="display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.65rem">
      <div v-if="!faces.length" style="color: var(--muted); font-size: 0.84rem">No one enrolled yet.</div>
      <div
        v-for="face in faces"
        :key="face.id"
        style="
          display: flex;
          align-items: center;
          gap: 0.6rem;
          padding: 0.4rem 0.6rem;
          background: var(--card-bg, rgba(255, 255, 255, 0.03));
          border-radius: 0.35rem;
        "
      >
        <span style="flex: 1; font-size: 0.85rem">{{ face.name }}</span>
        <button class="btn sm ghost" @click="remove(face.id)">Delete</button>
      </div>
    </div>
    <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap">
      <input v-model="name" class="tag-input" placeholder="Name" style="max-width: 12rem" />
      <input ref="fileInput" type="file" accept="image/*" />
      <button class="btn sm" :disabled="enrolling" @click="enroll">
        {{ enrolling ? '⏳ Enrolling…' : '+ Enroll' }}
      </button>
    </div>
  </div>
</template>
